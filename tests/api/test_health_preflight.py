"""Readiness probe + production fail-closed at construction + lifespan (issue #66 / #87 B4)."""

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


def test_production_create_app_fails_closed_eagerly_on_unsafe_config(monkeypatch):
    # conftest leaves an unsafe-for-prod baseline (sqlalchemy backend w/o a
    # database_url, sandbox fallback on, open registration, ...). Flipping
    # CUTAGENT_ENV=production must make create_app() itself fail closed: #87 B4
    # moved the aggregated preflight gate AHEAD of configure_app_state's eager
    # engine build, so unsafe construction fails with the full report (not the
    # opaque low-level DB error) and never returns a servable app. The lifespan
    # gate is kept as defense-in-depth but is unreachable once construction fails.
    monkeypatch.setenv("CUTAGENT_ENV", "production")
    with pytest.raises(RuntimeError) as excinfo:
        create_app()
    msg = str(excinfo.value)
    assert "preflight" in msg.lower()
    # The aggregated report carries the unsafe codes, not a single DB engine error.
    assert "registration_open" in msg


def test_preflight_fails_closed_before_seeding_admin(monkeypatch):
    """#66 regression: the fail-closed preflight must run BEFORE bootstrap seeds
    the local admin/viewer.

    The original API lifespan called ``bootstrap_sqlalchemy_storage()``
    (which seeds usr_admin/usr_viewer with dev-default credentials when
    ``seed_local_auth`` is on) *before* ``validate_startup_settings``. On an unsafe
    production deploy that meant the hardcoded admin was written into the prod DB
    and only *then* did startup refuse to serve — the credentials lingered. #87 B4
    moved the gate even earlier, into ``create_app()`` construction itself: the
    preflight now runs and fails closed before configure_app_state/lifespan, so
    bootstrap (which lives in the lifespan) never gets the chance to seed. This
    asserts that construction-time ordering invariant — preflight ran, bootstrap
    did not — which is strictly stronger than the original lifespan-time guard.
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

    monkeypatch.setattr(appmod, "bootstrap_sqlalchemy_storage", _spy_bootstrap)
    monkeypatch.setattr(appmod, "validate_startup_settings", _spy_preflight)
    monkeypatch.setenv("CUTAGENT_ENV", "production")  # conftest baseline is unsafe-for-prod

    # create_app() now fails closed eagerly (before returning a servable app), so
    # the construction-time gate — not lifespan — is what must raise here.
    with pytest.raises(RuntimeError):
        appmod.create_app()

    assert "preflight" in calls, "preflight gate never ran"
    assert "bootstrap" not in calls, (
        "bootstrap (admin/viewer seed) ran before the fail-closed preflight; "
        f"call order was {calls}"
    )
