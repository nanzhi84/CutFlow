from fastapi.testclient import TestClient

from apps.api.main import app, repository
from packages.core.auth.service import hash_registration_code


def test_login_sets_httponly_cookie_and_session_reads_user():
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert "cutagent_session=" in cookie
    assert "HttpOnly" in cookie

    session = client.get("/api/auth/session")
    assert session.status_code == 200, session.text
    assert session.json()["user"]["email"] == "admin@local.cutagent"
    assert session.json()["user"]["status"] == "active"
    assert session.json()["session_id"].startswith("sess_")


def test_bad_login_is_unauthorized():
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth.invalid_credentials"


def test_non_auth_api_requires_session_by_default():
    client = TestClient(app)
    response = client.get("/api/cases")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth.unauthorized"


def test_viewer_cannot_use_operator_or_admin_routes():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"email": "viewer@local.cutagent", "password": "local-viewer"},
    )
    assert login.status_code == 200, login.text

    create_case = client.post("/api/cases", json={"name": "Viewer forbidden case"})
    assert create_case.status_code == 403

    prepare_upload = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "broll",
            "filename": "viewer.txt",
            "content_type": "text/plain",
            "size_bytes": 1,
        },
    )
    assert prepare_upload.status_code == 403

    secrets = client.get("/api/secrets")
    assert secrets.status_code == 403


def test_idempotency_key_replays_successful_write_and_rejects_conflict():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text

    headers = {"Idempotency-Key": "case-create-replay"}
    first = client.post("/api/cases", json={"name": "Idempotent Case"}, headers=headers)
    assert first.status_code == 201, first.text
    replayed = client.post("/api/cases", json={"name": "Idempotent Case"}, headers=headers)
    assert replayed.status_code == 200, replayed.text  # spec 32.11: replay -> 200
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.json()["id"] == first.json()["id"]

    conflict = client.post("/api/cases", json={"name": "Different Case"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency.conflict"


def test_logout_revokes_session_cookie():
    client = TestClient(app)
    client.post("/api/auth/login", json={"email": "admin@local.cutagent", "password": "local-admin"})
    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200
    session = client.get("/api/auth/session")
    assert session.status_code == 401


def test_registration_code_assigns_role_and_tracks_usage():
    client = TestClient(app)
    code_id = repository().registration_code_hashes[hash_registration_code("reg_local_admin")]
    before = repository().registration_codes[code_id].used_count
    email = f"role-{before}@example.test"
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "display_name": "Role User",
            "registration_code": "reg_local_admin",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["user"]["role"] == "admin"
    assert repository().registration_codes[code_id].used_count == before + 1
