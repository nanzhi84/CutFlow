"""Concurrency / QPS limiter for provider invocations.

ProviderProfile.concurrency_key carries the intended backpressure grouping
(vendor account / quota bucket). This module enforces a bounded number of
in-flight provider calls per key so concurrent durable runs cannot fan out
unbounded TTS/ASR/VLM/LipSync/LLM requests at vendor quotas.

When CUTAGENT_REDIS_URL is configured, Redis is used as the shared coordination
layer: a per-key lease set limits concurrency and a per-key token bucket limits
QPS across API/worker processes. Without Redis, or after Redis fails, the module
falls back to the original per-process concurrency semaphore. The fallback is
intentionally not fail-open; QPS needs shared state and is therefore not enforced
without Redis.

Thread-safety: the gateway runs provider calls under the activity
ThreadPoolExecutor, so multiple threads enter concurrently. A module-level lock
guards lazy creation of per-key semaphores; the semaphores themselves are
``threading.BoundedSemaphore`` instances which are individually thread-safe.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

DEFAULT_MAX_INFLIGHT = 4
DEFAULT_MAX_QPS = 4
_ENV_VAR = "CUTAGENT_PROVIDER_MAX_INFLIGHT"
_QPS_ENV_VAR = "CUTAGENT_PROVIDER_MAX_QPS"
_REDIS_ENV_VAR = "CUTAGENT_REDIS_URL"
_DEFAULT_NAMESPACE = "cutagent"

logger = logging.getLogger(__name__)

_default_limiter: "DistributedRateLimiter | None" = None
_default_limiter_lock = threading.Lock()

_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1] - ARGV[4])
local inflight = redis.call('ZCARD', KEYS[1])
if inflight >= tonumber(ARGV[3]) then
  return {0, 50}
end
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[5])
local capacity = tonumber(ARGV[6])
local tokens = tonumber(redis.call('HGET', KEYS[2], 'tokens') or capacity)
local updated_at = tonumber(redis.call('HGET', KEYS[2], 'updated_at') or now)
if now > updated_at then
  tokens = math.min(capacity, tokens + ((now - updated_at) / 1000.0) * rate)
end
if tokens < 1 then
  redis.call('HSET', KEYS[2], 'tokens', tokens, 'updated_at', now)
  redis.call('PEXPIRE', KEYS[2], math.max(ARGV[4], 2000))
  return {0, math.ceil(((1 - tokens) / rate) * 1000)}
end
tokens = tokens - 1
redis.call('ZADD', KEYS[1], now, ARGV[2])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
redis.call('HSET', KEYS[2], 'tokens', tokens, 'updated_at', now)
redis.call('PEXPIRE', KEYS[2], math.max(ARGV[4], 2000))
return {1, 0}
"""


def _max_inflight() -> int:
    """Resolve the per-key in-flight cap from the environment.

    Read lazily (not at import time) so tests / deployments can set the env var
    before the first invocation. Invalid or non-positive values fall back to the
    sane default rather than disabling backpressure.
    """

    raw = os.getenv(_ENV_VAR)
    if raw is None:
        return DEFAULT_MAX_INFLIGHT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INFLIGHT
    return value if value > 0 else DEFAULT_MAX_INFLIGHT


def _max_qps() -> int:
    raw = os.getenv(_QPS_ENV_VAR)
    if raw is None:
        return DEFAULT_MAX_QPS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_QPS
    return value if value > 0 else DEFAULT_MAX_QPS


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


class DistributedRateLimiter:
    def __init__(
        self,
        *,
        redis_url: str | None = None,
        namespace: str = _DEFAULT_NAMESPACE,
        max_inflight: int | None = None,
        max_qps: int | None = None,
        lease_ttl_seconds: float = 30.0,
        acquire_sleep_seconds: float = 0.05,
        redis_client_factory: Callable[[str], Any] = _redis_client_from_url,
    ) -> None:
        self.redis_url = redis_url
        self.namespace = namespace.rstrip(":")
        self.max_inflight = max_inflight if max_inflight and max_inflight > 0 else _max_inflight()
        self.max_qps = max_qps if max_qps and max_qps > 0 else _max_qps()
        self.lease_ttl_ms = max(1000, int(lease_ttl_seconds * 1000))
        self.acquire_sleep_seconds = acquire_sleep_seconds
        self._registry_lock = threading.Lock()
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._degradation_lock = threading.Lock()
        self._degraded = False
        self._redis = None
        if redis_url:
            try:
                self._redis = redis_client_factory(redis_url)
            except Exception as exc:  # pragma: no cover - exact Redis error varies by env.
                self._degrade(exc)

    @contextmanager
    def slot(self, concurrency_key: str | None, provider_id: str) -> Iterator[None]:
        key = (concurrency_key or "").strip() or provider_id
        if self._redis is None:
            with self._local_slot(key):
                yield
            return

        lease_id = uuid4().hex
        acquired = False
        try:
            while not acquired:
                try:
                    wait_ms = self._try_acquire_redis_slot(key, lease_id)
                except Exception as exc:  # pragma: no cover - exercised by bad Redis envs.
                    self._degrade(exc)
                    with self._local_slot(key):
                        yield
                    return
                if wait_ms is None:
                    acquired = True
                    break
                time.sleep(max(self.acquire_sleep_seconds, wait_ms / 1000.0))
            yield
        finally:
            if acquired:
                self._release_redis_slot(key, lease_id)

    @contextmanager
    def _local_slot(self, key: str) -> Iterator[None]:
        sem = self._semaphore_for(key)
        sem.acquire()
        try:
            yield
        finally:
            sem.release()

    def _semaphore_for(self, key: str) -> threading.BoundedSemaphore:
        sem = self._semaphores.get(key)
        if sem is None:
            with self._registry_lock:
                sem = self._semaphores.get(key)
                if sem is None:
                    sem = threading.BoundedSemaphore(self.max_inflight)
                    self._semaphores[key] = sem
        return sem

    def _try_acquire_redis_slot(self, key: str, lease_id: str) -> int | None:
        now_ms = int(time.time() * 1000)
        result = self._redis.eval(
            _ACQUIRE_SCRIPT,
            2,
            self._leases_key(key),
            self._qps_key(key),
            now_ms,
            lease_id,
            self.max_inflight,
            self.lease_ttl_ms,
            self.max_qps,
            self.max_qps,
        )
        acquired, wait_ms = int(result[0]), int(result[1])
        return None if acquired == 1 else max(wait_ms, 1)

    def _release_redis_slot(self, key: str, lease_id: str) -> None:
        try:
            self._redis.zrem(self._leases_key(key), lease_id)
        except Exception as exc:  # pragma: no cover - best-effort lease cleanup.
            self._degrade(exc)

    def _leases_key(self, key: str) -> str:
        return f"{self.namespace}:provider:{key}:leases"

    def _qps_key(self, key: str) -> str:
        return f"{self.namespace}:provider:{key}:qps"

    def _degrade(self, exc: Exception) -> None:
        with self._degradation_lock:
            self._redis = None
            if self._degraded:
                return
            self._degraded = True
        logger.warning(
            "redis limiter degraded; using per-process provider concurrency limiter",
            extra={
                "event": "provider_limiter.redis_degraded",
                "degradation_level": "fail_safe",
                "redis_url_configured": bool(self.redis_url),
                "reason": str(exc),
            },
        )


def _get_default_limiter() -> DistributedRateLimiter:
    global _default_limiter
    limiter = _default_limiter
    if limiter is not None:
        return limiter
    with _default_limiter_lock:
        limiter = _default_limiter
        if limiter is None:
            limiter = DistributedRateLimiter(redis_url=os.getenv(_REDIS_ENV_VAR))
            _default_limiter = limiter
        return limiter


@contextmanager
def provider_slot(concurrency_key: str | None, provider_id: str) -> Iterator[None]:
    """Acquire one in-flight slot for the given concurrency key.

    Falls back to ``provider_id`` when ``concurrency_key`` is missing/blank so a
    profile without an explicit key is still bounded (rather than unbounded).
    """

    with _get_default_limiter().slot(concurrency_key, provider_id):
        yield


def reset_limiter_for_tests() -> None:
    """Clear the per-key semaphore registry (test isolation helper)."""

    global _default_limiter
    with _default_limiter_lock:
        _default_limiter = None
