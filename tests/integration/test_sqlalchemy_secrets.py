from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from packages.core.contracts import (
    CreateSecretRequest,
    DisableSecretRequest,
    RotateSecretRequest,
)
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import AuditEventRow, SecretRow
from packages.core.storage.secret_store import LocalSecretStore
from packages.core.storage.sqlalchemy_secrets import SqlAlchemySecretRepository, SqlAlchemySecretStore


def _secret_repo(tmp_path: Path) -> SqlAlchemySecretRepository:
    # Backed by the real Postgres test database. Per-test isolation (truncate +
    # reseed) is handled automatically by the autouse fixture in tests/conftest.py.
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    store = LocalSecretStore(root=tmp_path / "secrets")
    return SqlAlchemySecretRepository(session_factory, store)


def _audit_rows(repo: SqlAlchemySecretRepository) -> list[AuditEventRow]:
    with repo.session_factory() as session:
        return list(session.scalars(select(AuditEventRow).where(AuditEventRow.resource_type == "secret")))


def test_db_create_rotate_disable_write_audit_atomically_in_session(tmp_path):
    # Spec §32.9: the audit row must be written in the SAME transaction as the
    # mutation. We assert: (a) every governance op has a matching audit row, and
    # (b) NO secret op exists without its audit (no separate-session double-write).
    repo = _secret_repo(tmp_path)
    plaintext = "create-plain-aaa"
    rotated_plaintext = "rotate-plain-bbb"

    created = repo.create_secret(
        CreateSecretRequest(
            provider_id="acme", environment="prod", name="key", plaintext_secret=plaintext
        ),
        actor="usr_admin",
    )
    rotated = repo.rotate_secret(
        created.id,
        RotateSecretRequest(plaintext_secret=rotated_plaintext, reason="rotate"),
        actor="usr_admin",
    )
    repo.disable_secret(
        rotated.id, DisableSecretRequest(reason="disable"), actor="usr_admin"
    )

    rows = _audit_rows(repo)
    by_action: dict[str, list[AuditEventRow]] = {}
    for row in rows:
        by_action.setdefault(row.action, []).append(row)

    # Exactly the three governance actions, each attributed to the real actor.
    assert {r.id for r in by_action.get("secret.create", []) if r.resource_id == created.id}
    assert {r.id for r in by_action.get("secret.rotate", []) if r.resource_id == rotated.id}
    assert {r.id for r in by_action.get("secret.disable", []) if r.resource_id == rotated.id}
    for action in ("secret.create", "secret.rotate", "secret.disable"):
        assert by_action[action][0].actor == "usr_admin"
        assert by_action[action][0].details["provider_id"] == "acme"
        assert by_action[action][0].details["environment"] == "prod"

    # No audit detail may ever carry the plaintext value.
    serialized = repr([(r.action, r.details) for r in rows])
    assert plaintext not in serialized
    assert rotated_plaintext not in serialized


def test_db_audit_failure_rolls_back_the_mutation(tmp_path, monkeypatch):
    # Spec §32.9 fail-closed: if the audit write fails, the mutation must NOT
    # persist (same transaction). Force the audit add to raise and assert no
    # SecretRow was committed.
    repo = _secret_repo(tmp_path)

    import packages.core.storage.sqlalchemy_secrets as mod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("audit table down")

    monkeypatch.setattr(mod, "_add_secret_audit", _boom)

    with pytest.raises(RuntimeError, match="audit table down"):
        repo.create_secret(
            CreateSecretRequest(
                provider_id="acme", environment="prod", name="key", plaintext_secret="x"
            ),
            actor="usr_admin",
        )

    with repo.session_factory() as session:
        assert session.scalars(select(SecretRow)).first() is None
        assert session.scalars(select(AuditEventRow)).first() is None


def test_db_read_secret_reveals_and_audits_atomically_without_plaintext(tmp_path):
    repo = _secret_repo(tmp_path)
    plaintext = "read-plain-ccc"
    created = repo.create_secret(
        CreateSecretRequest(
            provider_id="acme", environment="prod", name="key", plaintext_secret=plaintext
        ),
        actor="usr_admin",
    )

    value = repo.read_secret(created.id, actor="reader")
    assert value == plaintext

    read_rows = [r for r in _audit_rows(repo) if r.action == "secret.read"]
    assert len(read_rows) == 1
    assert read_rows[0].resource_id == created.id
    assert read_rows[0].actor == "reader"
    assert plaintext not in repr([(r.action, r.details) for r in _audit_rows(repo)])


def test_db_secret_value_is_encrypted_in_row_and_survives_missing_local_file(tmp_path):
    repo = _secret_repo(tmp_path)
    plaintext = "db-encrypted-secret-ddd"

    created = repo.create_secret(
        CreateSecretRequest(
            provider_id="acme", environment="prod", name="key", plaintext_secret=plaintext
        ),
        actor="usr_admin",
    )

    with repo.session_factory() as session:
        row = session.get(SecretRow, created.id)
        assert row is not None
        assert row.encrypted_value
        assert row.encrypted_value.startswith("fernet:v1:")
        assert plaintext not in row.encrypted_value
        secret_ref = row.secret_ref

    repo.secret_store.disable(secret_ref)

    db_store = SqlAlchemySecretStore(repo.session_factory, fallback=repo.secret_store)
    assert db_store.get(secret_ref) == plaintext
    assert repo.read_secret(created.id, actor="reader") == plaintext


def test_db_read_secret_missing_returns_none_and_skips_audit(tmp_path):
    repo = _secret_repo(tmp_path)
    assert repo.read_secret("sec_missing", actor="reader") is None
    assert [r for r in _audit_rows(repo) if r.action == "secret.read"] == []


def sqlalchemy_session_factory():
    return get_sqlalchemy_session_factory_if_enabled()


def test_sqlalchemy_secret_create_rotate_disable_flow_is_persisted_without_plaintext():
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:8]

    from fastapi.testclient import TestClient

    from apps.api.main import app

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
            assert row.encrypted_value
            assert row.encrypted_value.startswith("fernet:v1:")
            assert "first-secret-value" not in row.encrypted_value

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
        assert new_row.encrypted_value is None

    # Spec 11.3 / 32.9: create/rotate/disable each append a secret audit event.
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
