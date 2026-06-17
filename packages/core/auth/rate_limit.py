"""In-process sliding-window brute-force limiter for auth (R2).

Tracks failed-login and registration attempts per ``(scope, client, identifier)``
key in process memory and rejects once the configured threshold inside the
window is exceeded. Thresholds/windows come from ``AuthSettings``
(``max_login_attempts`` / ``login_window_minutes`` / ``max_registration_attempts``
/ ``registration_window_minutes``) read freshly per call via ``build_settings``.

TODO(multi-worker): this limiter is PROCESS-LOCAL. Under multiple Gunicorn/
Uvicorn workers each process keeps its own counters, so the effective global
threshold is ``workers * max_attempts`` and an attacker who is load-balanced
across workers gets more tries. There is no shared attempts store in this repo
(no Redis, and adding a DB table was explicitly out of scope for the auth lane),
so this is the pragmatic in-process choice. For a hard global limit, back this
with Redis or a DB attempts table behind the same ``LoginRateLimiter`` /
``RegistrationRateLimiter`` interface.

A module-level :func:`reset` clears all buckets; tests call it in setup so
per-``TestClient`` runs do not leak attempt state into each other.
"""

from __future__ import annotations

import threading
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from packages.core.config import build_settings
from packages.core.contracts import ErrorCode, utcnow
from packages.core.workflow import NodeExecutionError

# Hard floors so a misconfigured (e.g. 0) env value never disables the limiter
# or sets a zero-length window. Mirrors the old repo's max(...) clamps.
_MIN_ATTEMPTS = 1
_MIN_WINDOW_MINUTES = 1
_DEFAULT_NAMESPACE = "cutagent"

logger = logging.getLogger(__name__)


def _redis_client_from_url(redis_url: str) -> Any:
    import redis

    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=1.0,
    )
    client.ping()
    return client


@dataclass
class _SlidingWindowLimiter:
    """Per-key sliding-window counter.

    ``buckets`` maps a composite key to the timestamps of recorded attempts
    within the active window; entries older than the window are pruned lazily on
    each access. Guarded by a lock so concurrent requests cannot race the
    read-modify-write."""

    _buckets: dict[str, list[datetime]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _redis_lock: threading.RLock = field(default_factory=threading.RLock)
    _redis_url: str | None = None
    _redis: Any = None
    _redis_failed: bool = False
    _redis_keys: set[str] = field(default_factory=set)

    def _prune(self, key: str, *, now: datetime, window: timedelta) -> list[datetime]:
        attempts = [item for item in self._buckets.get(key, []) if now - item < window]
        if attempts:
            self._buckets[key] = attempts
        else:
            self._buckets.pop(key, None)
        return attempts

    def check(
        self,
        key: str,
        *,
        max_attempts: int,
        window_minutes: int,
        redis_url: str | None = None,
    ) -> bool:
        """Return True if a further attempt is allowed (under the threshold)."""
        client = self._redis_client(redis_url)
        if client is not None:
            try:
                return self._redis_check(
                    client,
                    key,
                    max_attempts=max_attempts,
                    window_minutes=window_minutes,
                )
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
        now = utcnow()
        window = timedelta(minutes=max(_MIN_WINDOW_MINUTES, window_minutes))
        with self._lock:
            attempts = self._prune(key, now=now, window=window)
            return len(attempts) < max(_MIN_ATTEMPTS, max_attempts)

    def record(
        self,
        key: str,
        *,
        window_minutes: int,
        redis_url: str | None = None,
    ) -> None:
        """Record one attempt against ``key`` (used to count toward the limit)."""
        now = utcnow()
        window = timedelta(minutes=max(_MIN_WINDOW_MINUTES, window_minutes))
        with self._lock:
            attempts = self._prune(key, now=now, window=window)
            attempts.append(now)
            self._buckets[key] = attempts
        client = self._redis_client(redis_url)
        if client is not None:
            try:
                self._redis_record(client, key, window_minutes=window_minutes)
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)

    def clear_key(self, key: str, *, redis_url: str | None = None) -> None:
        with self._lock:
            self._buckets.pop(key, None)
        client = self._redis_client(redis_url)
        if client is not None:
            try:
                client.delete(self._redis_key(key))
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
        client = self._redis_client(self._redis_url)
        if client is None:
            return
        with self._redis_lock:
            keys = list(self._redis_keys)
            self._redis_keys.clear()
        if keys:
            try:
                client.delete(*keys)
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)

    def _redis_client(self, redis_url: str | None):
        if not redis_url:
            return None
        self._sync_redis_url(redis_url)
        if self._redis_failed:
            return None
        with self._redis_lock:
            if self._redis is not None:
                return self._redis
            try:
                self._redis = _redis_client_from_url(redis_url)
                return self._redis
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
                return None

    def _sync_redis_url(self, redis_url: str | None) -> None:
        if redis_url == self._redis_url:
            return
        with self._redis_lock:
            if redis_url == self._redis_url:
                return
            redis = self._redis
            self._redis = None
            self._redis_url = redis_url
            self._redis_failed = False
            self._redis_keys.clear()
        if redis is not None:
            try:
                redis.close()
            except Exception:
                pass

    def _redis_key(self, key: str) -> str:
        redis_key = f"{_DEFAULT_NAMESPACE}:auth-rate:{key}"
        with self._redis_lock:
            self._redis_keys.add(redis_key)
        return redis_key

    def _redis_check(
        self,
        client,
        key: str,
        *,
        max_attempts: int,
        window_minutes: int,
    ) -> bool:
        now_ms = int(time.time() * 1000)
        window_ms = max(_MIN_WINDOW_MINUTES, window_minutes) * 60 * 1000
        redis_key = self._redis_key(key)
        pipe = client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, now_ms - window_ms)
        pipe.zcard(redis_key)
        pipe.pexpire(redis_key, window_ms)
        result = pipe.execute()
        return int(result[1]) < max(_MIN_ATTEMPTS, max_attempts)

    def _redis_record(self, client, key: str, *, window_minutes: int) -> None:
        now_ms = int(time.time() * 1000)
        window_ms = max(_MIN_WINDOW_MINUTES, window_minutes) * 60 * 1000
        redis_key = self._redis_key(key)
        pipe = client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, now_ms - window_ms)
        pipe.zadd(redis_key, {f"{now_ms}:{uuid4().hex}": now_ms})
        pipe.pexpire(redis_key, window_ms)
        pipe.execute()

    def _degrade(self, exc: Exception) -> None:
        with self._redis_lock:
            if self._redis_failed:
                return
            self._redis_failed = True
            redis = self._redis
            self._redis = None
        if redis is not None:
            try:
                redis.close()
            except Exception:
                pass
        logger.warning(
            "redis auth rate limiter degraded; using per-process auth limiter",
            extra={
                "event": "auth.rate_limit.redis_degraded",
                "degradation_level": "fail_safe",
                "redis_url_configured": bool(self._redis_url),
                "reason": str(exc),
            },
        )


# Two independent buckets: failed logins vs registration attempts.
_login_limiter = _SlidingWindowLimiter()
_registration_limiter = _SlidingWindowLimiter()


def _normalize(value: str | None) -> str:
    return (value or "unknown").strip().lower()


def _login_key(client_id: str | None, identifier: str) -> str:
    return f"login:{_normalize(client_id)}:{_normalize(identifier)}"


def _registration_key(client_id: str | None) -> str:
    return f"register:{_normalize(client_id)}"


def check_login_rate_limit(client_id: str | None, identifier: str) -> None:
    """Raise if the login attempt is currently throttled.

    Uses ``ErrorCode.auth_invalid_credentials`` (401) with a distinct message so
    callers cannot distinguish throttling from a bad password (anti-enumeration)
    and no new ErrorCode is needed."""
    settings = build_settings()
    if not _login_limiter.check(
        _login_key(client_id, identifier),
        max_attempts=settings.auth.max_login_attempts,
        window_minutes=settings.auth.login_window_minutes,
        redis_url=settings.redis_url,
    ):
        raise NodeExecutionError(
            ErrorCode.auth_invalid_credentials,
            "Too many failed login attempts; try again later.",
        )


def record_login_failure(client_id: str | None, identifier: str) -> None:
    settings = build_settings()
    _login_limiter.record(
        _login_key(client_id, identifier),
        window_minutes=settings.auth.login_window_minutes,
        redis_url=settings.redis_url,
    )


def record_login_success(client_id: str | None, identifier: str) -> None:
    """Clear the failure counter on a successful login."""
    settings = build_settings()
    _login_limiter.clear_key(_login_key(client_id, identifier), redis_url=settings.redis_url)


def check_registration_rate_limit(client_id: str | None) -> None:
    """Raise if registration from this client is currently throttled.

    Uses ``ErrorCode.validation_invalid_options`` (400) per the locked lane."""
    settings = build_settings()
    if not _registration_limiter.check(
        _registration_key(client_id),
        max_attempts=settings.auth.max_registration_attempts,
        window_minutes=settings.auth.registration_window_minutes,
        redis_url=settings.redis_url,
    ):
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "Too many registration attempts; try again later.",
        )


def record_registration_attempt(client_id: str | None) -> None:
    settings = build_settings()
    _registration_limiter.record(
        _registration_key(client_id),
        window_minutes=settings.auth.registration_window_minutes,
        redis_url=settings.redis_url,
    )


def reset() -> None:
    """Clear ALL limiter state. Call in test setup to avoid cross-test leakage."""
    _login_limiter.reset()
    _registration_limiter.reset()
