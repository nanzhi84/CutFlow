from __future__ import annotations

from datetime import timedelta

import pytest

from packages.core.auth import rate_limit
from packages.core.contracts import utcnow
from packages.core.workflow import NodeExecutionError


@pytest.fixture(autouse=True)
def _reset_limiters():
    rate_limit.reset()
    yield
    rate_limit.reset()


class _FakeRedisPipeline:
    def __init__(self, client):
        self.client = client
        self.calls: list[tuple] = []

    def zremrangebyscore(self, *args):
        self.calls.append(("zremrangebyscore", *args))
        return self

    def zcard(self, *args):
        self.calls.append(("zcard", *args))
        return self

    def pexpire(self, *args):
        self.calls.append(("pexpire", *args))
        return self

    def zadd(self, *args):
        self.calls.append(("zadd", *args))
        return self

    def execute(self):
        self.client.pipelines.append(self.calls)
        return self.client.results.pop(0)


class _FakeRedis:
    def __init__(self, *results):
        self.results = list(results)
        self.pipelines: list[list[tuple]] = []
        self.deleted: list[tuple] = []
        self.closed = False

    def pipeline(self):
        return _FakeRedisPipeline(self)

    def delete(self, *keys):
        self.deleted.append(keys)

    def close(self):
        self.closed = True


def test_sliding_window_prunes_old_attempts_and_enforces_floor():
    limiter = rate_limit._SlidingWindowLimiter()
    key = "login:client:email"
    limiter._buckets[key] = [utcnow() - timedelta(minutes=3)]

    assert limiter.check(key, max_attempts=0, window_minutes=0) is True
    limiter.record(key, window_minutes=0)
    assert limiter.check(key, max_attempts=0, window_minutes=0) is False
    assert len(limiter._buckets[key]) == 1

    limiter.clear_key(key)
    assert limiter.check(key, max_attempts=1, window_minutes=1) is True


def test_redis_check_record_reset_and_url_switch(monkeypatch):
    clients = [
        _FakeRedis([0, 0, 60_000], [None, None, None]),
        _FakeRedis([None, 2, 60_000]),
    ]

    def fake_client_from_url(_url):
        return clients.pop(0)

    monkeypatch.setattr(rate_limit, "_redis_client_from_url", fake_client_from_url)
    limiter = rate_limit._SlidingWindowLimiter()

    assert limiter.check("key", max_attempts=1, window_minutes=1, redis_url="redis://one") is True
    limiter.record("key", window_minutes=1, redis_url="redis://one")
    first_client = limiter._redis
    assert any(call[0] == "zcard" for call in first_client.pipelines[0])
    assert any(call[0] == "zadd" for call in first_client.pipelines[1])

    limiter.reset()
    assert first_client.deleted

    assert limiter.check("key", max_attempts=1, window_minutes=1, redis_url="redis://two") is False
    assert first_client.closed is True


def test_redis_degradation_falls_back_and_logs_once(monkeypatch, caplog):
    class BrokenRedis(_FakeRedis):
        def pipeline(self):
            raise RuntimeError("redis down")

    broken = BrokenRedis()
    monkeypatch.setattr(rate_limit, "_redis_client_from_url", lambda _url: broken)
    limiter = rate_limit._SlidingWindowLimiter()

    assert limiter.check("key", max_attempts=1, window_minutes=1, redis_url="redis://bad") is True
    assert limiter._redis_failed is True
    assert broken.closed is True
    assert "auth rate limiter degraded" in caplog.text

    caplog.clear()
    limiter.record("key", window_minutes=1, redis_url="redis://bad")
    assert caplog.text == ""


def test_public_login_and_registration_helpers_raise_expected_errors(monkeypatch):
    monkeypatch.setenv("CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS", "1")
    monkeypatch.setenv("CUTAGENT_AUTH_LOGIN_WINDOW_MINUTES", "1")
    monkeypatch.setenv("CUTAGENT_AUTH_MAX_REGISTRATION_ATTEMPTS", "1")
    monkeypatch.setenv("CUTAGENT_AUTH_REGISTRATION_WINDOW_MINUTES", "1")
    monkeypatch.delenv("CUTAGENT_REDIS_URL", raising=False)

    rate_limit.record_login_failure(" Client ", " USER@EXAMPLE.COM ")
    with pytest.raises(NodeExecutionError) as login_exc:
        rate_limit.check_login_rate_limit("client", "user@example.com")
    assert login_exc.value.error.code.value == "auth.invalid_credentials"

    rate_limit.record_login_success("client", "user@example.com")
    rate_limit.check_login_rate_limit("client", "user@example.com")

    rate_limit.record_registration_attempt(None)
    with pytest.raises(NodeExecutionError) as registration_exc:
        rate_limit.check_registration_rate_limit(None)
    assert registration_exc.value.error.code.value == "validation.invalid_options"


def test_redis_connection_failure_uses_local_bucket(monkeypatch):
    monkeypatch.setattr(
        rate_limit,
        "_redis_client_from_url",
        lambda _url: (_ for _ in ()).throw(RuntimeError("no redis")),
    )
    limiter = rate_limit._SlidingWindowLimiter()

    limiter.record("key", window_minutes=1, redis_url="redis://missing")
    assert limiter.check("key", max_attempts=1, window_minutes=1, redis_url="redis://missing") is False
