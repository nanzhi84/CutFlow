from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from queue import Empty, Queue
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import utcnow
from packages.core.observability.telemetry import (
    record_redis_degraded,
    record_redis_reconnect_attempt,
    record_redis_recovered,
)
from packages.core.storage.database import OutboxEventRow

logger = logging.getLogger(__name__)

# After Redis degrades, the next coordination op past this cooldown triggers one
# reconnect attempt; on success the layer rejoins Redis (issue #67). Lazy (no
# background thread) so it never hammers a down Redis.
REDIS_RECONNECT_COOLDOWN_SECONDS = 30.0


def _build_redis_client(redis_url: str):
    import redis

    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=1.0,
    )
    client.ping()
    return client


class InProcessFanoutHub:
    def __init__(
        self,
        *,
        redis_url: str | None = None,
        namespace: str = "cutagent",
        redis_client_factory: Callable[[str], Any] = _build_redis_client,
    ) -> None:
        self._subscribers: dict[str, list[Queue]] = {}
        self._lock = threading.RLock()
        self._redis_url = redis_url
        self._namespace = namespace.rstrip(":")
        self._instance_id = uuid4().hex
        self._redis = None
        self._redis_failed = False
        self._degraded_at: float | None = None
        self._redis_client_factory = redis_client_factory
        self._redis_lock = threading.RLock()
        self._pubsubs: dict[str, Any] = {}
        self._subscription_stops: dict[str, threading.Event] = {}
        self._subscription_threads: dict[str, threading.Thread] = {}
        self._closed = threading.Event()

    def subscribe(self, run_id: str) -> Queue:
        subscriber: Queue = Queue()
        with self._lock:
            self._subscribers.setdefault(run_id, []).append(subscriber)
        if self._redis_url:
            self._ensure_subscription(run_id)
        return subscriber

    def unsubscribe(self, run_id: str, subscriber: Queue) -> None:
        should_stop = False
        with self._lock:
            subscribers = self._subscribers.get(run_id, [])
            if subscriber in subscribers:
                subscribers.remove(subscriber)
            if not subscribers:
                self._subscribers.pop(run_id, None)
                should_stop = True
        if should_stop:
            self._stop_subscription(run_id)

    def publish(self, run_id: str, payload: dict[str, Any]) -> None:
        self._fanout_local(run_id, payload)
        if self._redis_url:
            self._publish_redis(run_id, payload)

    def _fanout_local(self, run_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers.get(run_id, []))
        for subscriber in subscribers:
            subscriber.put(payload)

    def get_nowait(self, subscriber: Queue) -> dict[str, Any] | None:
        try:
            return subscriber.get_nowait()
        except Empty:
            return None

    def close(self) -> None:
        self._closed.set()
        with self._redis_lock:
            pubsubs = list(self._pubsubs.values())
            stops = list(self._subscription_stops.values())
            threads = list(self._subscription_threads.values())
            self._pubsubs.clear()
            self._subscription_stops.clear()
            self._subscription_threads.clear()
            redis = self._redis
            self._redis = None
        for stop in stops:
            stop.set()
        for pubsub in pubsubs:
            try:
                pubsub.close()
            except Exception:
                pass
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)
        if redis is not None:
            try:
                redis.close()
            except Exception:
                pass

    def _channel(self, run_id: str) -> str:
        return f"{self._namespace}:run:{run_id}"

    def _redis_client(self):
        if not self._redis_url or self._closed.is_set():
            return None
        with self._redis_lock:
            if self._redis is not None:
                return self._redis
            if self._redis_failed and not self._reconnect_due():
                return None
            reconnecting = self._redis_failed
            if reconnecting:
                # Cooldown elapsed: attempt to rejoin Redis (lazy reconnect).
                self._redis_failed = False
                record_redis_reconnect_attempt("event_fanout")
            try:
                client = self._redis_client_factory(self._redis_url)
                self._redis = client
                if self._degraded_at is not None:
                    record_redis_recovered("event_fanout")
                    self._degraded_at = None
                return client
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
                return None

    def _reconnect_due(self) -> bool:
        return (
            self._degraded_at is not None
            and (time.monotonic() - self._degraded_at) >= REDIS_RECONNECT_COOLDOWN_SECONDS
        )

    def is_redis_degraded(self) -> bool:
        """Whether Redis is configured but this fanout has fallen back to
        per-process delivery (cross-replica broadcast is currently broken)."""
        return bool(self._redis_url) and self._redis_failed

    def _ensure_subscription(self, run_id: str) -> None:
        if self._closed.is_set():
            return
        with self._redis_lock:
            if run_id in self._subscription_threads:
                return
        client = self._redis_client()
        if client is None:
            return
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(self._channel(run_id))
        except Exception as exc:  # pragma: no cover - exact Redis errors vary.
            self._degrade(exc)
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=self._listen_to_subscription,
            args=(run_id, pubsub, stop),
            name=f"cutagent-event-fanout-{run_id}",
            daemon=True,
        )
        with self._redis_lock:
            if run_id in self._subscription_threads or self._closed.is_set():
                stop.set()
                try:
                    pubsub.close()
                except Exception:
                    pass
                return
            self._pubsubs[run_id] = pubsub
            self._subscription_stops[run_id] = stop
            self._subscription_threads[run_id] = thread
        thread.start()

    def _stop_subscription(self, run_id: str) -> None:
        with self._redis_lock:
            pubsub = self._pubsubs.pop(run_id, None)
            stop = self._subscription_stops.pop(run_id, None)
            thread = self._subscription_threads.pop(run_id, None)
        if stop is not None:
            stop.set()
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    def _listen_to_subscription(self, run_id: str, pubsub, stop: threading.Event) -> None:
        while not self._closed.is_set() and not stop.is_set():
            try:
                message = pubsub.get_message(timeout=0.1)
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                if not self._closed.is_set() and not stop.is_set():
                    self._degrade(exc)
                break
            if not message or message.get("type") != "message":
                continue
            try:
                envelope = json.loads(message.get("data") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if envelope.get("instance_id") == self._instance_id:
                continue
            payload = envelope.get("payload")
            if isinstance(payload, dict):
                self._fanout_local(run_id, payload)

    def _publish_redis(self, run_id: str, payload: dict[str, Any]) -> None:
        client = self._redis_client()
        if client is None:
            return
        try:
            client.publish(
                self._channel(run_id),
                json.dumps(
                    {"instance_id": self._instance_id, "payload": payload},
                    separators=(",", ":"),
                ),
            )
        except Exception as exc:  # pragma: no cover - exact Redis errors vary.
            self._degrade(exc)

    def _degrade(self, exc: Exception) -> None:
        with self._redis_lock:
            if self._redis_failed:
                return
            self._redis_failed = True
            self._degraded_at = time.monotonic()
            record_redis_degraded("event_fanout")
            redis = self._redis
            self._redis = None
            pubsubs = list(self._pubsubs.values())
            stops = list(self._subscription_stops.values())
            self._pubsubs.clear()
            self._subscription_stops.clear()
            self._subscription_threads.clear()
        for stop in stops:
            stop.set()
        for pubsub in pubsubs:
            try:
                pubsub.close()
            except Exception:
                pass
        if redis is not None:
            try:
                redis.close()
            except Exception:
                pass
        logger.warning(
            "redis event fanout degraded; using per-process fanout",
            extra={
                "event": "observability.event_fanout.redis_degraded",
                "degradation_level": "fail_safe",
                "redis_url_configured": bool(self._redis_url),
                "reason": str(exc),
            },
        )


class SqlAlchemyOutboxDispatcher:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        hub: InProcessFanoutHub,
        poll_interval_seconds: float = 0.2,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.hub = hub
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self.dispatch_once()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stopped.set()

    async def dispatch_once(self) -> int:
        now = utcnow()
        published = 0
        with self.session_factory() as session:
            # Claim pending rows. On Postgres use FOR UPDATE SKIP LOCKED so that with
            # N dispatcher replicas each pending row is claimed by exactly one worker
            # (no double-publish / lost websocket tail). SQLite lacks SKIP LOCKED, so we
            # fall back to a plain SELECT there (single-process tests only).
            claim = (
                select(OutboxEventRow)
                .where(OutboxEventRow.status == "pending")
                .where(OutboxEventRow.available_at <= now)
                .order_by(OutboxEventRow.created_at, OutboxEventRow.id)
                .limit(self.batch_size)
            )
            if session.get_bind().dialect.name == "postgresql":
                claim = claim.with_for_update(skip_locked=True)
            rows = list(session.scalars(claim))
            for row in rows:
                try:
                    payload = row.payload if isinstance(row.payload, dict) else {"payload": row.payload}
                    run_id = str(payload.get("run_id") or row.aggregate_id)
                    self.hub.publish(run_id, payload)
                    row.status = "published"
                    row.attempts += 1
                    row.published_at = now
                    row.updated_at = now
                    published += 1
                except Exception as exc:  # pragma: no cover - injected hub failures only.
                    row.attempts += 1
                    row.available_at = now + timedelta(seconds=min(60, 2**row.attempts))
                    row.last_error = str(exc)
                    row.updated_at = now
            _update_sqlalchemy_outbox_lag(session, now)
            session.commit()
        return published


def replay_sqlalchemy_outbox(
    session_factory: sessionmaker[Session],
    *,
    aggregate_type: str,
    aggregate_id: str,
    after_event_id: str | None = None,
) -> list[dict[str, Any]]:
    """Replay an aggregate's outbox events in (created_at, id) order.

    When ``after_event_id`` is given (#87 D2 cursor resume), only events strictly
    after that cursor are returned. The outbox row PK *is* the ``RunEvent.event_id``
    carried in the payload, so the cursor row's (created_at, id) sort position is a
    direct lookup. An unknown id (never persisted / pruned) falls back to a full
    replay — harmless because the client dedups against its already-seen ids.
    """
    with session_factory() as session:
        statement = (
            select(OutboxEventRow)
            .where(OutboxEventRow.aggregate_type == aggregate_type)
            .where(OutboxEventRow.aggregate_id == aggregate_id)
            .order_by(OutboxEventRow.created_at, OutboxEventRow.id)
        )
        if after_event_id is not None:
            cursor = session.get(OutboxEventRow, after_event_id)
            if cursor is not None:
                statement = statement.where(
                    or_(
                        OutboxEventRow.created_at > cursor.created_at,
                        and_(
                            OutboxEventRow.created_at == cursor.created_at,
                            OutboxEventRow.id > cursor.id,
                        ),
                    )
                )
        rows = list(session.scalars(statement))
    return [row.payload for row in rows if isinstance(row.payload, dict)]


def _update_sqlalchemy_outbox_lag(session: Session, now) -> None:
    oldest = session.scalar(
        select(OutboxEventRow.created_at)
        .where(OutboxEventRow.status == "pending")
        .order_by(OutboxEventRow.created_at)
        .limit(1)
    )
    if oldest is None:
        from packages.core.observability.telemetry import OUTBOX_LAG

        OUTBOX_LAG.set(0)
        return
    from packages.core.observability.telemetry import OUTBOX_LAG

    if oldest.tzinfo is None and now.tzinfo is not None:
        oldest = oldest.replace(tzinfo=now.tzinfo)
    OUTBOX_LAG.set(max(0, (now - oldest).total_seconds()))


@dataclass(frozen=True)
class EventStreamToken:
    token: str
    run_id: str
    expires_at: datetime


class EventStreamTokenStore:
    def __init__(
        self,
        *,
        redis_url: str | None = None,
        namespace: str = "cutagent",
        redis_client_factory: Callable[[str], Any] = _build_redis_client,
    ) -> None:
        self._tokens: dict[str, EventStreamToken] = {}
        self._redis_url = redis_url
        self._namespace = namespace.rstrip(":")
        self._redis = None
        self._redis_failed = False
        self._degraded_at: float | None = None
        self._redis_client_factory = redis_client_factory
        self._redis_lock = threading.RLock()

    def issue(self, run_id: str, ttl: timedelta) -> EventStreamToken:
        token = f"stream_{uuid4().hex[:24]}"
        issued = EventStreamToken(token=token, run_id=run_id, expires_at=utcnow() + ttl)
        self._tokens[token] = issued
        client = self._redis_client()
        if client is not None:
            try:
                client.set(self._key(token), run_id, px=max(1, int(ttl.total_seconds() * 1000)))
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
        return issued

    def validate(self, token: str, run_id: str) -> bool:
        client = self._redis_client()
        if client is not None:
            try:
                return client.get(self._key(token)) == run_id
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
        issued = self._tokens.get(token)
        if issued is None:
            return False
        if issued.run_id != run_id:
            return False
        if issued.expires_at < utcnow():
            self._tokens.pop(token, None)
            return False
        return True

    def _key(self, token: str) -> str:
        return f"{self._namespace}:event-token:{token}"

    def _redis_client(self):
        if not self._redis_url:
            return None
        with self._redis_lock:
            if self._redis is not None:
                return self._redis
            if self._redis_failed and not self._reconnect_due():
                return None
            if self._redis_failed:
                self._redis_failed = False
                record_redis_reconnect_attempt("event_token_store")
            try:
                client = self._redis_client_factory(self._redis_url)
                self._redis = client
                if self._degraded_at is not None:
                    record_redis_recovered("event_token_store")
                    self._degraded_at = None
                return client
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
                return None

    def _reconnect_due(self) -> bool:
        return (
            self._degraded_at is not None
            and (time.monotonic() - self._degraded_at) >= REDIS_RECONNECT_COOLDOWN_SECONDS
        )

    def is_redis_degraded(self) -> bool:
        """Whether Redis is configured but tokens are being issued/validated from
        per-process state (cross-replica token validation is currently broken)."""
        return bool(self._redis_url) and self._redis_failed

    def _degrade(self, exc: Exception) -> None:
        with self._redis_lock:
            if self._redis_failed:
                return
            self._redis_failed = True
            self._degraded_at = time.monotonic()
            record_redis_degraded("event_token_store")
            redis = self._redis
            self._redis = None
        if redis is not None:
            try:
                redis.close()
            except Exception:
                pass
        logger.warning(
            "redis event token store degraded; using per-process token store",
            extra={
                "event": "observability.event_tokens.redis_degraded",
                "degradation_level": "fail_safe",
                "redis_url_configured": bool(self._redis_url),
                "reason": str(exc),
            },
        )


async def receive_from_subscriber(subscriber: Queue, timeout: float = 0.05) -> dict[str, Any] | None:
    try:
        return subscriber.get_nowait()
    except Empty:
        await asyncio.sleep(timeout)
        return None
