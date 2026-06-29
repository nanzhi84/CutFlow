"""Redis degradation metrics + lazy reconnect (issue #67).

Pure unit tests: a fake ``redis_client_factory`` is injected so no real Redis is
needed. They exercise degrade -> is_redis_degraded -> cooldown reconnect ->
recover, the reconnect-attempt counter, and the CUTAGENT_REDIS_REQUIRED setting.
"""

from __future__ import annotations

from packages.ai.gateway import provider_limiter
from packages.ai.gateway.provider_limiter import DistributedRateLimiter
from packages.core.config import build_settings
from packages.core.observability import events
from packages.core.observability.events import EventStreamTokenStore, InProcessFanoutHub
from packages.core.observability.telemetry import REDIS_RECONNECT_ATTEMPTS


class _FakeRedis:
    def close(self) -> None:  # used by _degrade cleanup paths
        pass


def _fail_once_then_ok():
    state = {"n": 0}

    def factory(_url: str):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("redis down")
        return _FakeRedis()

    return factory, state


def _reconnect_count(component: str) -> float:
    return REDIS_RECONNECT_ATTEMPTS.labels(component=component)._value.get()


def test_fanout_hub_degrades_then_reconnects_after_cooldown(monkeypatch):
    monkeypatch.setattr(events, "REDIS_RECONNECT_COOLDOWN_SECONDS", 0.0)
    factory, state = _fail_once_then_ok()
    hub = InProcessFanoutHub(redis_url="redis://fake", redis_client_factory=factory)

    before = _reconnect_count("event_fanout")
    # First connect attempt fails -> degraded to per-process fanout.
    assert hub._redis_client() is None
    assert hub.is_redis_degraded() is True

    # Cooldown is 0, so the next call reconnects (lazy reconnect).
    assert hub._redis_client() is not None
    assert hub.is_redis_degraded() is False
    assert state["n"] == 2
    assert _reconnect_count("event_fanout") == before + 1


def test_token_store_degrades_then_reconnects_after_cooldown(monkeypatch):
    monkeypatch.setattr(events, "REDIS_RECONNECT_COOLDOWN_SECONDS", 0.0)
    factory, state = _fail_once_then_ok()
    store = EventStreamTokenStore(redis_url="redis://fake", redis_client_factory=factory)

    assert store._redis_client() is None
    assert store.is_redis_degraded() is True
    assert store._redis_client() is not None
    assert store.is_redis_degraded() is False
    assert state["n"] == 2


def test_fanout_hub_respects_cooldown_before_reconnecting(monkeypatch):
    # A large cooldown means a degraded hub does NOT hammer a down Redis.
    monkeypatch.setattr(events, "REDIS_RECONNECT_COOLDOWN_SECONDS", 10_000.0)
    factory, state = _fail_once_then_ok()
    hub = InProcessFanoutHub(redis_url="redis://fake", redis_client_factory=factory)
    assert hub._redis_client() is None  # degrade (attempt 1)
    assert hub._redis_client() is None  # still in cooldown -> no new attempt
    assert state["n"] == 1
    assert hub.is_redis_degraded() is True


def test_provider_limiter_degrades_then_reconnects(monkeypatch):
    monkeypatch.setattr(provider_limiter, "_REDIS_RECONNECT_COOLDOWN_SECONDS", 0.0)
    factory, state = _fail_once_then_ok()
    limiter = DistributedRateLimiter(redis_url="redis://fake", redis_client_factory=factory)
    # __init__ connect failed -> degraded.
    assert limiter.is_redis_degraded() is True
    assert state["n"] == 1

    limiter._maybe_reconnect()
    assert limiter.is_redis_degraded() is False
    assert state["n"] == 2


def test_redis_not_configured_is_never_degraded():
    # No redis_url -> per-process by design, not a degradation.
    hub = InProcessFanoutHub(redis_url=None)
    store = EventStreamTokenStore(redis_url=None)
    limiter = DistributedRateLimiter(redis_url=None)
    assert hub.is_redis_degraded() is False
    assert store.is_redis_degraded() is False
    assert limiter.is_redis_degraded() is False


def test_redis_required_setting_reads_env(monkeypatch):
    monkeypatch.setenv("CUTAGENT_REDIS_REQUIRED", "true")
    assert build_settings().redis_required is True
    monkeypatch.setenv("CUTAGENT_REDIS_REQUIRED", "0")
    assert build_settings().redis_required is False
    monkeypatch.delenv("CUTAGENT_REDIS_REQUIRED", raising=False)
    assert build_settings().redis_required is False
