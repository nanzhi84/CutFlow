from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import timedelta

from packages.core.observability import events as event_mod
from packages.core.observability.events import EventStreamTokenStore, InProcessFanoutHub


def test_local_fanout_subscribe_unsubscribe_lifecycle() -> None:
    hub = InProcessFanoutHub()
    first = hub.subscribe("run_local")
    second = hub.subscribe("run_local")

    hub.publish("run_local", {"event_id": "evt_1"})
    assert hub.get_nowait(first)["event_id"] == "evt_1"
    assert hub.get_nowait(second)["event_id"] == "evt_1"

    hub.unsubscribe("run_local", first)
    hub.publish("run_local", {"event_id": "evt_2"})

    assert hub.get_nowait(first) is None
    assert hub.get_nowait(second)["event_id"] == "evt_2"
    hub.unsubscribe("run_local", second)
    hub.close()


class _FakePubSub:
    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.closed = False

    def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    def get_message(self, timeout: float = 0.1):
        if not self.closed:
            time.sleep(min(timeout, 0.01))
        return None

    def close(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self) -> None:
        self.pubsubs: list[_FakePubSub] = []
        self.published: list[tuple[str, str]] = []
        self.closed = False

    def pubsub(self, *, ignore_subscribe_messages: bool):
        assert ignore_subscribe_messages is True
        pubsub = _FakePubSub()
        self.pubsubs.append(pubsub)
        return pubsub

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))

    def close(self) -> None:
        self.closed = True


class _BrokenCloseRedis(_FakeRedis):
    def close(self) -> None:
        self.closed = True
        raise RuntimeError("close failed")


def test_redis_fanout_uses_channel_envelope_and_closes_resources() -> None:
    redis = _FakeRedis()
    hub = InProcessFanoutHub(
        redis_url="redis://fake",
        namespace="testns",
        redis_client_factory=lambda _url: redis,
    )

    subscriber = hub.subscribe("run_remote")
    hub.publish("run_remote", {"event_id": "evt_remote"})
    hub.unsubscribe("run_remote", subscriber)
    hub.close()

    assert redis.pubsubs[0].subscribed == ["testns:run:run_remote"]
    channel, raw = redis.published[0]
    assert channel == "testns:run:run_remote"
    envelope = json.loads(raw)
    assert envelope["payload"] == {"event_id": "evt_remote"}
    assert envelope["instance_id"]
    assert redis.pubsubs[0].closed is True
    assert redis.closed is True


def test_redis_fanout_degrades_then_reconnects_after_cooldown(monkeypatch) -> None:
    redis = _FakeRedis()
    calls = 0
    degraded: list[str] = []
    recovered: list[str] = []
    reconnects: list[str] = []

    def factory(_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("redis down")
        return redis

    monkeypatch.setattr(event_mod, "record_redis_degraded", degraded.append)
    monkeypatch.setattr(event_mod, "record_redis_recovered", recovered.append)
    monkeypatch.setattr(event_mod, "record_redis_reconnect_attempt", reconnects.append)

    hub = InProcessFanoutHub(
        redis_url="redis://fake",
        namespace="testns",
        redis_client_factory=factory,
    )
    subscriber = hub.subscribe("run_degraded")

    assert hub.is_redis_degraded() is True
    assert degraded == ["event_fanout"]
    assert hub._redis_client() is None

    hub._degraded_at = time.monotonic() - event_mod.REDIS_RECONNECT_COOLDOWN_SECONDS - 1
    hub.publish("run_degraded", {"event_id": "evt_after_reconnect"})

    assert reconnects == ["event_fanout"]
    assert recovered == ["event_fanout"]
    assert redis.published
    assert hub.get_nowait(subscriber)["event_id"] == "evt_after_reconnect"
    hub.close()


def test_redis_fanout_listener_filters_envelopes_and_degrade_closes_resources(monkeypatch) -> None:
    hub = InProcessFanoutHub(namespace="testns")
    subscriber = hub.subscribe("run_remote")
    stop = threading.Event()

    class SequencedPubSub:
        def __init__(self) -> None:
            self.messages = [
                None,
                {"type": "subscribe", "data": "{}"},
                {"type": "message", "data": "{bad json"},
                {
                    "type": "message",
                    "data": json.dumps({"instance_id": hub._instance_id, "payload": {"event_id": "self"}}),
                },
                {"type": "message", "data": json.dumps({"instance_id": "remote", "payload": ["bad"]})},
                {
                    "type": "message",
                    "data": json.dumps({"instance_id": "remote", "payload": {"event_id": "remote"}}),
                },
            ]

        def get_message(self, timeout: float = 0.1):
            if not self.messages:
                stop.set()
                return None
            return self.messages.pop(0)

    hub._listen_to_subscription("run_remote", SequencedPubSub(), stop)
    assert hub.get_nowait(subscriber) == {"event_id": "remote"}
    assert hub.get_nowait(subscriber) is None

    pubsub = _FakePubSub()
    redis = _BrokenCloseRedis()
    hub._redis = redis
    hub._pubsubs["run_remote"] = pubsub
    hub._subscription_stops["run_remote"] = threading.Event()
    monkeypatch.setattr(event_mod, "record_redis_degraded", lambda _scope: None)
    hub._degrade(RuntimeError("publish failed"))

    assert hub.is_redis_degraded() is False
    assert pubsub.closed is True
    assert redis.closed is True


def test_local_token_store_validates_run_and_expires_tokens() -> None:
    store = EventStreamTokenStore()
    valid = store.issue("run_a", timedelta(minutes=5))
    expired = store.issue("run_a", timedelta(seconds=-1))

    assert store.validate(valid.token, "run_a") is True
    assert store.validate(valid.token, "run_b") is False
    assert store.validate("missing", "run_a") is False
    assert store.validate(expired.token, "run_a") is False
    assert expired.token not in store._tokens


class _FakeTokenRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int]] = []
        self.closed = False
        self.fail_get = False
        self.fail_set = False

    def set(self, key: str, value: str, *, px: int) -> None:
        if self.fail_set:
            raise RuntimeError("set failed")
        self.values[key] = value
        self.set_calls.append((key, value, px))

    def get(self, key: str):
        if self.fail_get:
            raise RuntimeError("get failed")
        return self.values.get(key)

    def close(self) -> None:
        self.closed = True


def test_redis_token_store_sets_validates_degrades_and_reconnects(monkeypatch) -> None:
    redis = _FakeTokenRedis()
    degraded: list[str] = []
    recovered: list[str] = []
    reconnects: list[str] = []

    monkeypatch.setattr(event_mod, "record_redis_degraded", degraded.append)
    monkeypatch.setattr(event_mod, "record_redis_recovered", recovered.append)
    monkeypatch.setattr(event_mod, "record_redis_reconnect_attempt", reconnects.append)

    store = EventStreamTokenStore(
        redis_url="redis://fake",
        namespace="testns",
        redis_client_factory=lambda _url: redis,
    )
    issued = store.issue("run_redis", timedelta(seconds=10))
    key = f"testns:event-token:{issued.token}"

    assert redis.set_calls == [(key, "run_redis", 10000)]
    assert store.validate(issued.token, "run_redis") is True
    redis.values[key] = "other_run"
    assert store.validate(issued.token, "run_redis") is False

    redis.values[key] = "run_redis"
    redis.fail_get = True
    assert store.validate(issued.token, "run_redis") is True
    assert store.is_redis_degraded() is True
    assert degraded == ["event_token_store"]
    assert redis.closed is True

    redis.fail_get = False
    store._degraded_at = time.monotonic() - event_mod.REDIS_RECONNECT_COOLDOWN_SECONDS - 1
    second = store.issue("run_redis", timedelta(seconds=1))

    assert second.token != issued.token
    assert reconnects == ["event_token_store"]
    assert recovered == ["event_token_store"]


def test_redis_token_store_factory_failure_falls_back_to_local(monkeypatch) -> None:
    calls = 0
    redis = _FakeTokenRedis()

    def factory(_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("redis unavailable")
        return redis

    monkeypatch.setattr(event_mod, "record_redis_degraded", lambda _scope: None)
    store = EventStreamTokenStore(redis_url="redis://fake", redis_client_factory=factory)
    issued = store.issue("run_local_fallback", timedelta(seconds=10))

    assert store.is_redis_degraded() is True
    assert store.validate(issued.token, "run_local_fallback") is True

    store._degraded_at = time.monotonic() - event_mod.REDIS_RECONNECT_COOLDOWN_SECONDS - 1
    reconnected = store._redis_client()

    assert reconnected is redis
    assert calls == 2


def test_receive_from_subscriber_waits_then_returns_none() -> None:
    hub = InProcessFanoutHub()
    subscriber = hub.subscribe("run_empty")

    assert asyncio.run(event_mod.receive_from_subscriber(subscriber, timeout=0)) is None
