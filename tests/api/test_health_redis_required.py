"""Readiness fail-closed when CUTAGENT_REDIS_REQUIRED is set (issue #87 B2 / #81).

When Redis is *required*, a replica whose Redis-backed singletons (event fan-out
hub, event-stream token store, provider rate limiter) have fallen back to their
per-process degraded mode must report not-ready (503) so the orchestrator drains
it. When Redis is *not* required (the default), the same degradation is tolerated
in place (fail-safe) and readiness stays 200.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app


def test_readiness_503_when_required_and_event_hub_degraded(monkeypatch):
    monkeypatch.setenv("CUTAGENT_REDIS_REQUIRED", "true")
    app = create_app()
    client = TestClient(app)

    # Required but nothing degraded -> still ready.
    healthy = client.get("/api/health/ready")
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["redis_required"] is True
    assert healthy.json()["redis_degradations"] == []

    # Degrade the event hub -> not-ready (503), naming the degraded component.
    monkeypatch.setattr(app.state.event_hub, "is_redis_degraded", lambda: True)
    degraded = client.get("/api/health/ready")
    assert degraded.status_code == 503, degraded.text
    body = degraded.json()
    assert body["status"] == "not_ready"
    assert "event_hub" in body["redis_degradations"]


def test_readiness_503_when_required_and_provider_limiter_degraded(monkeypatch):
    monkeypatch.setenv("CUTAGENT_REDIS_REQUIRED", "true")
    monkeypatch.setattr(
        "apps.api.services.core.default_limiter_redis_degraded", lambda: True
    )
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/health/ready")
    assert response.status_code == 503, response.text
    assert "provider_limiter" in response.json()["redis_degradations"]


def test_readiness_ignores_degradation_when_redis_not_required(monkeypatch):
    monkeypatch.delenv("CUTAGENT_REDIS_REQUIRED", raising=False)  # default = not required
    app = create_app()
    client = TestClient(app)

    # Even with a degraded component, a non-required deploy degrades in place (200).
    monkeypatch.setattr(app.state.event_hub, "is_redis_degraded", lambda: True)
    response = client.get("/api/health/ready")
    assert response.status_code == 200, response.text
    assert response.json()["redis_required"] is False
    assert response.json()["redis_degradations"] == []
