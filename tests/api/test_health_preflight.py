"""Readiness probe + production fail-closed lifespan (issue #66)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app


def test_health_ready_is_public_and_ready_outside_production():
    app = create_app()
    client = TestClient(app)
    response = client.get("/api/health/ready")  # no auth — operational probe
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["preflight_issues"] == []


def test_production_lifespan_fails_closed_on_unsafe_config(monkeypatch):
    # conftest leaves an unsafe-for-prod baseline (memory backend, sandbox
    # fallback on, open registration, ...). Flipping CUTAGENT_ENV=production must
    # make the lifespan startup preflight fail closed rather than serve.
    monkeypatch.setenv("CUTAGENT_ENV", "production")
    app = create_app()
    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(app):
            pass
    assert "preflight" in str(excinfo.value).lower()


def test_preflight_fails_closed_before_seeding_admin(monkeypatch):
    """#66 regression: the fail-closed preflight must run BEFORE bootstrap seeds
    the local admin/viewer.

    The original API lifespan called ``bootstrap_sqlalchemy_storage_if_enabled()``
    (which seeds usr_admin/usr_viewer with dev-default credentials when
    ``seed_local_auth`` is on) *before* ``validate_startup_settings``. On an unsafe
    production deploy that meant the hardcoded admin was written into the prod DB
    and only *then* did startup refuse to serve — the credentials lingered. The
    worker had the correct order; this asserts the API now mirrors it: no seeding
    side effect may occur before the gate fails closed.
    """
    import apps.api.app as appmod

    calls: list[str] = []
    real_preflight = appmod.validate_startup_settings

    def _spy_bootstrap(*args, **kwargs):
        # Record only — never actually seed; we assert ordering, not the seed.
        calls.append("bootstrap")

    def _spy_preflight(settings):
        # Call through so the gate genuinely evaluates the unsafe prod config.
        calls.append("preflight")
        return real_preflight(settings)

    monkeypatch.setattr(appmod, "bootstrap_sqlalchemy_storage_if_enabled", _spy_bootstrap)
    monkeypatch.setattr(appmod, "validate_startup_settings", _spy_preflight)
    monkeypatch.setenv("CUTAGENT_ENV", "production")  # conftest baseline is unsafe-for-prod

    app = appmod.create_app()
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass

    assert "preflight" in calls, "preflight gate never ran"
    assert "bootstrap" not in calls, (
        "bootstrap (admin/viewer seed) ran before the fail-closed preflight; "
        f"call order was {calls}"
    )
