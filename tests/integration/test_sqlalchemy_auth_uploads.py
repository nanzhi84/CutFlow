import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient


from apps.api.main import app
from packages.core.auth.service import hash_registration_code
from packages.core.auth.sqlalchemy_service import hash_session_token
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import ArtifactRow, RegistrationCodeRow, SessionRow, UploadSessionRow, UserRow


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def test_sqlalchemy_auth_login_session_and_logout_are_persisted():
    session_factory = sqlalchemy_session_factory()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert login.status_code == 200, login.text
        token = client.cookies.get("cutagent_session")
        assert token

        # R3: the raw cookie token is NOT the PK; the stored PK is its sha256.
        hashed = hash_session_token(token)
        assert hashed != token

        with session_factory() as session:
            # raw token must NOT be a key; hashed token must be.
            assert session.get(SessionRow, token) is None
            row = session.get(SessionRow, hashed)
            assert row is not None
            assert row.user_id == "usr_admin"
            assert row.revoked_at is None

        session_response = client.get("/api/auth/session")
        assert session_response.status_code == 200, session_response.text
        assert session_response.json()["user"]["email"] == "admin@local.cutagent"

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200, logout.text
        assert client.get("/api/auth/session").status_code == 401

        with session_factory() as session:
            row = session.get(SessionRow, hashed)
            assert row is not None
            assert row.revoked_at is not None


def test_sqlalchemy_upload_and_artifact_flow_are_persisted():
    session_factory = sqlalchemy_session_factory()
    content = b"sqlalchemy upload payload"
    digest = hashlib.sha256(content).hexdigest()

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        prepared = client.post(
            "/api/uploads/prepare",
            json={
                "kind": "broll",
                "filename": "db-sample.txt",
                "content_type": "text/plain",
                "size_bytes": len(content),
                "sha256": digest,
            },
        )
        assert prepared.status_code == 201, prepared.text
        upload = prepared.json()
        assert upload["object_uri"].startswith("local://")

        with session_factory() as session:
            row = session.get(UploadSessionRow, upload["id"])
            assert row is not None
            assert row.status == "prepared"

        uploaded = client.put(
            f"/api/uploads/{upload['id']}/file",
            files={"file": ("db-sample.txt", content, "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["status"] == "uploading"

        completed = client.post(
            "/api/uploads/complete",
            json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
        )
        assert completed.status_code == 200, completed.text
        body = completed.json()
        assert body["upload_session"]["status"] == "completed"
        assert body["artifact"]["kind"] == "uploaded.file"

        with session_factory() as session:
            upload_row = session.get(UploadSessionRow, upload["id"])
            artifact_row = session.get(ArtifactRow, body["artifact"]["artifact_id"])
            assert upload_row is not None
            assert upload_row.status == "completed"
            assert artifact_row is not None
            assert artifact_row.uri == upload["object_uri"]
            assert artifact_row.payload["filename"] == "db-sample.txt"


def test_sqlalchemy_auth_admin_users_and_registration_codes_are_persisted():
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:10]

    with TestClient(app) as client:
        viewer_login = client.post(
            "/api/auth/login",
            json={"email": "viewer@local.cutagent", "password": "local-viewer"},
        )
        assert viewer_login.status_code == 200, viewer_login.text
        forbidden = client.get("/api/auth/users")
        assert forbidden.status_code == 403

        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        created_user = client.post(
            "/api/auth/users",
            json={
                "email": f"operator-{suffix}@example.test",
                "password": "correct horse battery staple",
                "display_name": "Operator User",
                "role": "operator",
            },
        )
        assert created_user.status_code == 201, created_user.text
        user = created_user.json()
        assert user["role"] == "operator"

        patched_user = client.patch(
            f"/api/auth/users/{user['id']}",
            json={"display_name": "Disabled Operator", "status": "disabled"},
        )
        assert patched_user.status_code == 200, patched_user.text
        assert patched_user.json()["status"] == "disabled"

        listed_users = client.get("/api/auth/users", params={"limit": 200})
        assert listed_users.status_code == 200, listed_users.text
        assert any(item["id"] == user["id"] for item in listed_users.json()["items"])

        created_code = client.post(
            "/api/auth/registration-codes",
            json={"role": "viewer", "max_uses": 1},
        )
        assert created_code.status_code == 201, created_code.text
        code = created_code.json()
        assert code["status"] == "active"

        patched_code = client.patch(
            f"/api/auth/registration-codes/{code['id']}",
            json={"status": "disabled"},
        )
        assert patched_code.status_code == 200, patched_code.text
        assert patched_code.json()["status"] == "disabled"

        listed_codes = client.get("/api/auth/registration-codes")
        assert listed_codes.status_code == 200, listed_codes.text
        assert any(item["id"] == code["id"] for item in listed_codes.json()["items"])

    with session_factory() as session:
        user_row = session.get(UserRow, user["id"])
        code_row = session.get(RegistrationCodeRow, code["id"])
        assert user_row is not None
        assert user_row.display_name == "Disabled Operator"
        assert user_row.status == "disabled"
        assert code_row is not None
        assert code_row.status == "disabled"


def test_sqlalchemy_seeded_registration_code_uses_hash_not_plaintext_key():
    session_factory = sqlalchemy_session_factory()
    expected_hash = hash_registration_code("reg_local_admin")

    with session_factory() as session:
        row = session.scalar(select(RegistrationCodeRow).where(RegistrationCodeRow.code_hash == expected_hash))
        assert row is not None
        assert row.id != "reg_local_admin"
        assert row.code_hash != "reg_local_admin"


def test_sqlalchemy_auth_me_update_and_change_password_are_persisted():
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:10]
    email = f"self-service-{suffix}@example.test"
    old_password = "old password 123"
    new_password = "new password 456"

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        created_user = client.post(
            "/api/auth/users",
            json={
                "email": email,
                "password": old_password,
                "display_name": "Self Service User",
                "role": "operator",
            },
        )
        assert created_user.status_code == 201, created_user.text
        user_id = created_user.json()["id"]

        login = client.post("/api/auth/login", json={"email": email, "password": old_password})
        assert login.status_code == 200, login.text

        patched_me = client.patch("/api/auth/me", json={"display_name": "Renamed Self"})
        assert patched_me.status_code == 200, patched_me.text
        assert patched_me.json()["display_name"] == "Renamed Self"

        changed = client.post(
            "/api/auth/me/change-password",
            json={"old_password": old_password, "new_password": new_password},
        )
        assert changed.status_code == 200, changed.text

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200, logout.text

        old_login = client.post("/api/auth/login", json={"email": email, "password": old_password})
        assert old_login.status_code == 401

        new_login = client.post("/api/auth/login", json={"email": email, "password": new_password})
        assert new_login.status_code == 200, new_login.text

    with session_factory() as session:
        user_row = session.get(UserRow, user_id)
        assert user_row is not None
        assert user_row.display_name == "Renamed Self"


def test_sqlalchemy_change_password_revokes_other_sessions_but_keeps_current():
    # R5: changing the password must revoke every OTHER active session of the user
    # (so a leaked cookie dies) while keeping the caller's own session alive.
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:10]
    email = f"multi-session-{suffix}@example.test"
    old_password = "old password 123"
    new_password = "new password 456"

    with TestClient(app) as admin_client:
        admin_login = admin_client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text
        created = admin_client.post(
            "/api/auth/users",
            json={
                "email": email,
                "password": old_password,
                "display_name": "Multi Session User",
                "role": "operator",
            },
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]

        # Two independent sessions (distinct cookie jars) for the same user.
        client_a = TestClient(app)
        client_b = TestClient(app)
        assert client_a.post("/api/auth/login", json={"email": email, "password": old_password}).status_code == 200
        assert client_b.post("/api/auth/login", json={"email": email, "password": old_password}).status_code == 200
        token_b = client_b.cookies.get("cutagent_session")
        assert client_a.get("/api/auth/me").status_code == 200
        assert client_b.get("/api/auth/me").status_code == 200

        # Session A changes the password keeping its OWN cookie.
        changed = client_a.post(
            "/api/auth/me/change-password",
            json={"old_password": old_password, "new_password": new_password},
        )
        assert changed.status_code == 200, changed.text

        # Current session (A) survives; the other session (B) is revoked.
        assert client_a.get("/api/auth/me").status_code == 200
        assert client_b.get("/api/auth/me").status_code == 401

    with session_factory() as session:
        revoked_row = session.get(SessionRow, hash_session_token(token_b))
        assert revoked_row is not None
        assert revoked_row.revoked_at is not None
        # All of this user's sessions except the kept one are revoked.
        active = session.scalars(
            select(SessionRow)
            .where(SessionRow.user_id == user_id)
            .where(SessionRow.revoked_at.is_(None))
        ).all()
        assert len(active) == 1


def test_sqlalchemy_last_active_admin_is_guarded_on_db_backend():
    # R4 (SQL backend): the last active admin cannot be demoted or disabled, while
    # demotion IS allowed once another active admin exists. Written to be
    # non-polluting: usr_admin is never actually demoted/disabled (those patches are
    # rejected), so later tests can still authenticate as the seeded admin.
    session_factory = sqlalchemy_session_factory()
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        ).status_code == 200

        # Defensive: ensure usr_admin is the only active admin (demote any other
        # active admins left behind by earlier tests; the seed only has usr_admin).
        users = client.get("/api/auth/users", params={"limit": 500}).json()["items"]
        for user in users:
            if user["id"] != "usr_admin" and user["role"] == "admin" and user["status"] == "active":
                client.patch(f"/api/auth/users/{user['id']}", json={"role": "viewer"})

        # Last active admin: demote and disable are both rejected (no state change).
        demote = client.patch("/api/auth/users/usr_admin", json={"role": "viewer"})
        assert demote.status_code == 409
        assert demote.json()["error"]["code"] == "validation.conflict"
        disable = client.patch("/api/auth/users/usr_admin", json={"status": "disabled"})
        assert disable.status_code == 409
        assert disable.json()["error"]["code"] == "validation.conflict"

        # Allowed path: with a second active admin, demoting THAT one succeeds and
        # leaves usr_admin as the active admin.
        suffix = uuid4().hex[:10]
        second = client.post(
            "/api/auth/users",
            json={
                "email": f"second-admin-{suffix}@example.test",
                "password": "correct horse battery staple",
                "display_name": f"Second Admin {suffix}",
                "role": "admin",
            },
        )
        assert second.status_code == 201, second.text
        second_id = second.json()["id"]
        demote_second = client.patch(f"/api/auth/users/{second_id}", json={"role": "viewer"})
        assert demote_second.status_code == 200, demote_second.text
        assert demote_second.json()["role"] == "viewer"

    with session_factory() as session:
        admin_row = session.get(UserRow, "usr_admin")
        assert admin_row is not None
        assert admin_row.role == "admin"
        assert admin_row.status == "active"
