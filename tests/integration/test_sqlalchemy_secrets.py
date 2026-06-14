import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

if os.getenv("CUTAGENT_RUN_DB_TESTS") != "1":
    pytest.skip("Set CUTAGENT_RUN_DB_TESTS=1 to run database integration tests.", allow_module_level=True)

from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import AuditEventRow, SecretRow


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def test_sqlalchemy_secret_create_rotate_disable_flow_is_persisted_without_plaintext():
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:8]

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        created = client.post(
            "/api/secrets",
            json={
                "provider_id": f"sandbox-{suffix}",
                "environment": "local",
                "name": "API key",
                "plaintext_secret": "first-secret-value",
            },
        )
        assert created.status_code == 201, created.text
        secret = created.json()
        assert secret["masked_value"] == "********"
        assert secret["status"] == "active"

        with session_factory() as session:
            row = session.get(SecretRow, secret["id"])
            assert row is not None
            first_secret_ref = row.secret_ref
            assert first_secret_ref
            assert "first-secret-value" not in first_secret_ref
            assert not hasattr(row, "encrypted_value")

        rotated = client.post(
            f"/api/secrets/{secret['id']}/rotate",
            json={"plaintext_secret": "second-secret-value", "reason": "integration rotation"},
        )
        assert rotated.status_code == 200, rotated.text
        rotated_secret = rotated.json()
        assert rotated_secret["masked_value"] == "********"
        # Spec 11.3: rotation creates a NEW record linked to the old one.
        assert rotated_secret["id"] != secret["id"]
        assert rotated_secret["rotated_from_secret_id"] == secret["id"]
        assert rotated_secret["status"] == "active"

        disabled = client.patch(
            f"/api/secrets/{rotated_secret['id']}/disable",
            json={"reason": "integration disable"},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "disabled"

        listed = client.get("/api/secrets")
        assert listed.status_code == 200, listed.text
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert {secret["id"], rotated_secret["id"]} <= listed_ids

    with session_factory() as session:
        old_row = session.get(SecretRow, secret["id"])
        assert old_row is not None
        assert old_row.status == "rotated"
        assert old_row.rotated_at is not None
        assert old_row.secret_ref == first_secret_ref

        new_row = session.get(SecretRow, rotated_secret["id"])
        assert new_row is not None
        assert new_row.status == "disabled"
        assert new_row.rotated_from_secret_id == secret["id"]
        assert new_row.secret_ref != first_secret_ref
        assert "second-secret-value" not in new_row.secret_ref

    # Spec 11.3 / 32.9: create/rotate/disable each append a secret audit event.
    from sqlalchemy import select

    with session_factory() as session:
        rows = list(
            session.scalars(
                select(AuditEventRow).where(AuditEventRow.resource_type == "secret")
            )
        )
    by_action: dict[str, list[AuditEventRow]] = {}
    for row in rows:
        by_action.setdefault(row.action, []).append(row)

    create_events = [r for r in by_action.get("secret.create", []) if r.resource_id == secret["id"]]
    assert create_events, "missing secret.create audit event"
    create_event = create_events[0]
    # Actor is the authenticated admin user (not the bare "system" default).
    assert create_event.actor
    assert create_event.actor != "system"
    assert create_event.details.get("provider_id") == f"sandbox-{suffix}"
    assert create_event.details.get("environment") == "local"

    rotate_events = [
        r for r in by_action.get("secret.rotate", []) if r.resource_id == rotated_secret["id"]
    ]
    assert rotate_events, "missing secret.rotate audit event"

    disable_events = [
        r for r in by_action.get("secret.disable", []) if r.resource_id == rotated_secret["id"]
    ]
    assert disable_events, "missing secret.disable audit event"

    # No audit detail payload may ever carry the plaintext value.
    serialized = repr([(r.action, r.details) for r in rows])
    assert "first-secret-value" not in serialized
    assert "second-secret-value" not in serialized
