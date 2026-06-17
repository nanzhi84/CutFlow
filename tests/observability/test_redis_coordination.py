from __future__ import annotations

import logging
import os
import threading
import time
from datetime import timedelta
from uuid import uuid4

import pytest

from packages.ai.gateway import provider_limiter
from packages.core.observability.events import EventStreamTokenStore, InProcessFanoutHub

REDIS_URL = os.getenv("CUTAGENT_REDIS_URL")
if not REDIS_URL:
    pytest.skip("Set CUTAGENT_REDIS_URL to run Redis coordination tests.", allow_module_level=True)


@pytest.fixture()
def redis_client():
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"Redis is not reachable: {exc}")
    return client


def _namespace() -> str:
    return f"cutagent:test:{uuid4().hex}"


def test_limiter_instances_share_redis_concurrency_and_qps(redis_client) -> None:
    namespace = _namespace()
    limiter_a = provider_limiter.DistributedRateLimiter(
        redis_url=REDIS_URL,
        namespace=namespace,
        max_inflight=1,
        max_qps=1,
        acquire_sleep_seconds=0.01,
    )
    limiter_b = provider_limiter.DistributedRateLimiter(
        redis_url=REDIS_URL,
        namespace=namespace,
        max_inflight=1,
        max_qps=1,
        acquire_sleep_seconds=0.01,
    )

    entered = threading.Event()

    def acquire_second_slot() -> None:
        with limiter_b.slot("provider:shared", "provider.fake"):
            entered.set()

    with limiter_a.slot("provider:shared", "provider.fake"):
        thread = threading.Thread(target=acquire_second_slot)
        thread.start()
        assert not entered.wait(0.1)

    assert entered.wait(2)
    thread.join(timeout=2)

    started = time.monotonic()
    with limiter_a.slot("provider:qps", "provider.fake"):
        pass
    with limiter_b.slot("provider:qps", "provider.fake"):
        pass

    assert time.monotonic() - started >= 0.8
    redis_client.delete(f"{namespace}:provider:provider:qps:leases")
    redis_client.delete(f"{namespace}:provider:provider:qps:qps")


def test_token_store_issues_cross_instance_tokens_with_ttl(redis_client) -> None:
    namespace = _namespace()
    store_a = EventStreamTokenStore(redis_url=REDIS_URL, namespace=namespace)
    store_b = EventStreamTokenStore(redis_url=REDIS_URL, namespace=namespace)

    issued = store_a.issue("run_redis_token", timedelta(milliseconds=250))

    assert store_b.validate(issued.token, "run_redis_token")
    time.sleep(0.4)
    assert not store_b.validate(issued.token, "run_redis_token")
    redis_client.delete(f"{namespace}:event-token:{issued.token}")


def test_fanout_publishes_between_hub_instances(redis_client) -> None:
    namespace = _namespace()
    hub_a = InProcessFanoutHub(redis_url=REDIS_URL, namespace=namespace)
    hub_b = InProcessFanoutHub(redis_url=REDIS_URL, namespace=namespace)
    subscriber = hub_b.subscribe("run_redis_fanout")
    try:
        time.sleep(0.1)
        hub_a.publish("run_redis_fanout", {"event_id": "evt_redis_fanout"})
        deadline = time.monotonic() + 2
        received = None
        while time.monotonic() < deadline:
            received = hub_b.get_nowait(subscriber)
            if received is not None:
                break
            time.sleep(0.02)

        assert received == {"event_id": "evt_redis_fanout"}
    finally:
        hub_b.unsubscribe("run_redis_fanout", subscriber)
        hub_a.close()
        hub_b.close()


def test_limiter_degrades_to_per_process_when_redis_unreachable(caplog) -> None:
    caplog.set_level(logging.WARNING)
    limiter = provider_limiter.DistributedRateLimiter(
        redis_url="redis://127.0.0.1:6390",
        namespace=_namespace(),
        max_inflight=1,
        max_qps=1,
        acquire_sleep_seconds=0.01,
    )
    entered = threading.Event()

    def acquire_second_slot() -> None:
        with limiter.slot("provider:bad-redis", "provider.fake"):
            entered.set()

    with limiter.slot("provider:bad-redis", "provider.fake"):
        thread = threading.Thread(target=acquire_second_slot)
        thread.start()
        assert not entered.wait(0.1)

    assert entered.wait(2)
    thread.join(timeout=2)
    assert any("redis limiter degraded" in record.getMessage() for record in caplog.records)
