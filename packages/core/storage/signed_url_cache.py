"""Cache of presigned GET URLs so the same object always yields the same URL (issue #206).

Every OSS/S3 presigner folds a fresh wall-clock timestamp into the signature
(``Expires`` for the OSS V1 signer, ``X-Amz-Date`` for SigV4), so signing the
same object twice one second apart produces two *different* URLs. The browser
caches by full URL including query, so a polling list page that re-signs its
covers on every poll re-downloads every cover on every poll — the mechanism
behind the 884 GB / month OSS egress in issue #206.

The fix is to make the URL *stable*: sign once, hand the same string back until
it approaches expiry. Object keys are ``{purpose}/{uuid4 | sha256}/{name}`` —
a new version is always a new key and the bytes at a key never change — so the
signed URL for a key is safe to cache for as long as the signature is valid.

Redis owns the cache when ``CUTAGENT_REDIS_URL`` is configured, so every API
replica hands out the same URL; on Redis failure the cache degrades to a
per-process LRU (explicitly reported, never silent) and the URL stays stable
within each replica. Degrading only costs egress, never correctness, so this
layer deliberately does NOT participate in ``/api/health/ready`` shedding.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from packages.core.contracts import SignedUrlResponse, utcnow

logger = logging.getLogger(__name__)


def _telemetry():
    # Imported lazily: packages.core.observability imports the storage layer, so a
    # module-level import here would close an import cycle.
    from packages.core.observability import telemetry

    return telemetry


_COMPONENT = "signed_url_cache"
_NAMESPACE = "cutagent"
REDIS_RECONNECT_COOLDOWN_SECONDS = 30.0

# Re-sign once less than half of the TTL is left. This fraction is load-bearing
# for the Cache-Control we hand the browser (``object_store._cache_control``):
#   - a URL is served for at most (1 - f) * TTL before it rotates, and
#   - a URL we hand out always has at least f * TTL of validity left.
# Setting ``max-age = f * TTL`` therefore guarantees BOTH that the browser never
# re-requests a URL while it is still current (max-age >= rotation period, which
# needs f >= 0.5) AND that it never re-requests a URL whose signature has already
# expired (max-age <= remaining validity, which needs f <= 0.5). f = 0.5 is the
# only value that satisfies both — do not change one without the other.
REFRESH_FRACTION = 0.5

_DEFAULT_MAX_ENTRIES = 4096


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


def cache_key(uri: str, ttl: timedelta, response_content_disposition: str | None) -> str:
    """Identity of a signed URL: the object plus everything baked into its signature.

    ``response_content_disposition`` is part of the signature (an OSS signed
    sub-resource), so a preview URL and an attachment-download URL of the same
    object are different cached entries.
    """
    return f"{uri}|{int(ttl.total_seconds())}|{response_content_disposition or ''}"


class SignedUrlCache:
    """Redis-coordinated, per-process-fallback cache of presigned GET URLs."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        redis_client_factory: Callable[[str], Any] = _redis_client_from_url,
    ) -> None:
        self._redis_url = redis_url or None
        self._max_entries = max(1, max_entries)
        self._redis_client_factory = redis_client_factory
        self._local: OrderedDict[str, tuple[str, datetime]] = OrderedDict()
        self._lock = threading.RLock()
        self._redis_lock = threading.RLock()
        self._redis: Any = None
        self._redis_failed = False
        self._degraded_at: float | None = None

    def get_or_sign(
        self,
        key: str,
        *,
        ttl: timedelta,
        sign: Callable[[], SignedUrlResponse],
    ) -> SignedUrlResponse:
        """Return the cached signed URL for ``key``, signing only when needed.

        Signs when the key has never been signed, or when the cached signature
        has less than ``REFRESH_FRACTION`` of its TTL left.
        """
        cached = self._get(key, ttl=ttl)
        if cached is not None:
            url, expires_at = cached
            return SignedUrlResponse(url=url, expires_at=expires_at, request_id="req_cached")
        signed = sign()
        self._put(key, signed, ttl=ttl)
        return signed

    def clear(self) -> None:
        """Drop all cached entries (tests / explicit invalidation)."""
        with self._lock:
            self._local.clear()

    # -- lookup ---------------------------------------------------------------

    def _get(self, key: str, *, ttl: timedelta) -> tuple[str, datetime] | None:
        client = self._redis_client()
        if client is not None:
            try:
                raw = client.get(self._redis_key(key))
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
            else:
                entry = _decode(raw)
                if entry is not None and self._is_fresh(entry[1], ttl=ttl):
                    return entry
                return None
        with self._lock:
            entry = self._local.get(key)
            if entry is None:
                return None
            if not self._is_fresh(entry[1], ttl=ttl):
                self._local.pop(key, None)
                return None
            self._local.move_to_end(key)
            return entry

    def _put(self, key: str, signed: SignedUrlResponse, *, ttl: timedelta) -> None:
        client = self._redis_client()
        if client is not None:
            try:
                client.set(
                    self._redis_key(key),
                    json.dumps(
                        {"url": signed.url, "expires_at": signed.expires_at.isoformat()},
                        separators=(",", ":"),
                    ),
                    ex=max(1, int(ttl.total_seconds() * (1.0 - REFRESH_FRACTION))),
                )
                return
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
        with self._lock:
            self._local[key] = (signed.url, signed.expires_at)
            self._local.move_to_end(key)
            while len(self._local) > self._max_entries:
                self._local.popitem(last=False)

    @staticmethod
    def _is_fresh(expires_at: datetime, *, ttl: timedelta) -> bool:
        return (expires_at - utcnow()) > ttl * REFRESH_FRACTION

    # -- Redis ----------------------------------------------------------------

    def _redis_key(self, key: str) -> str:
        return f"{_NAMESPACE}:signed-url:{key}"

    def _redis_client(self) -> Any:
        if not self._redis_url:
            return None
        with self._redis_lock:
            if self._redis is not None:
                return self._redis
            if self._redis_failed and not self._reconnect_due():
                return None
            if self._redis_failed:
                self._redis_failed = False
                _telemetry().record_redis_reconnect_attempt(_COMPONENT)
            try:
                client = self._redis_client_factory(self._redis_url)
            except Exception as exc:  # pragma: no cover - exact Redis errors vary.
                self._degrade(exc)
                return None
            self._redis = client
            if self._degraded_at is not None:
                _telemetry().record_redis_recovered(_COMPONENT)
                self._degraded_at = None
            return client

    def _reconnect_due(self) -> bool:
        return (
            self._degraded_at is not None
            and (time.monotonic() - self._degraded_at) >= REDIS_RECONNECT_COOLDOWN_SECONDS
        )

    def is_redis_degraded(self) -> bool:
        """Whether Redis is configured but this cache fell back to per-process LRU.

        Cross-replica URL stability is broken while this is True (each replica
        hands out its own stable URL), which costs extra egress but nothing else.
        """
        return bool(self._redis_url) and self._redis_failed

    def _degrade(self, exc: Exception) -> None:
        with self._redis_lock:
            if self._redis_failed:
                return
            self._redis_failed = True
            self._degraded_at = time.monotonic()
            _telemetry().record_redis_degraded(_COMPONENT)
            redis = self._redis
            self._redis = None
        if redis is not None:
            try:
                redis.close()
            except Exception:
                pass
        logger.warning(
            "redis signed-url cache degraded; using per-process signed-url cache",
            extra={
                "event": "storage.signed_url_cache.redis_degraded",
                "degradation_level": "fail_safe",
                "redis_url_configured": bool(self._redis_url),
                "reason": str(exc),
            },
        )


def _decode(raw: Any) -> tuple[str, datetime] | None:
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
        return str(payload["url"]), datetime.fromisoformat(str(payload["expires_at"]))
    except Exception:
        return None


__all__ = ["SignedUrlCache", "REFRESH_FRACTION", "cache_key"]
