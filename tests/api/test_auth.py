import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.app import create_app
from apps.api.main import app
from packages.core.auth import rate_limit
from packages.core.auth.service import hash_registration_code
from packages.core.storage.database import RegistrationCodeRow


def _registration_code_used_count(code: str) -> int:
    """Read a registration code's ``used_count`` from Postgres (the SQL backend is
    now the only storage backend; the in-memory repo no longer tracks codes)."""
    with app.state.sqlalchemy_session_factory() as session:
        row = session.scalar(
            select(RegistrationCodeRow).where(
                RegistrationCodeRow.code_hash == hash_registration_code(code)
            )
        )
        assert row is not None, f"registration code {code} not seeded"
        return row.used_count


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # The brute-force limiter is module-global; reset before each test so
    # per-TestClient attempt counts never leak across tests.
    rate_limit.reset()
    yield
    rate_limit.reset()


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
    # R3: the SQL auth service stores only the HASH of the session token, so the
    # /session endpoint intentionally returns an empty session_id (the raw token is
    # known only to the client). The issued raw token — which still carries the
    # ``sess_`` prefix — lives in the cookie, so assert it there.
    assert "cutagent_session=sess_" in cookie


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
            "kind": "video",
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
    before = _registration_code_used_count("reg_local_admin")
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
    assert _registration_code_used_count("reg_local_admin") == before + 1


def test_admin_created_registration_code_returns_plaintext_once_and_can_register():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text

    created = client.post(
        "/api/auth/registration-codes",
        json={"role": "operator", "max_uses": 1},
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    plaintext_code = created_body["plaintext_code"]
    assert isinstance(plaintext_code, str)
    assert plaintext_code.startswith("reg_code_")

    listed = client.get("/api/auth/registration-codes")
    assert listed.status_code == 200, listed.text
    listed_code = next(item for item in listed.json()["items"] if item["id"] == created_body["id"])
    assert "plaintext_code" not in listed_code

    registered = client.post(
        "/api/auth/register",
        json={
            "email": f"{created_body['id']}@example.test",
            "password": "correct horse battery staple",
            "display_name": "Issued Code User",
            "registration_code": plaintext_code,
        },
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["user"]["role"] == "operator"

    reused = client.post(
        "/api/auth/register",
        json={
            "email": f"{created_body['id']}-again@example.test",
            "password": "correct horse battery staple",
            "display_name": "Reuse Code User",
            "registration_code": plaintext_code,
        },
    )
    assert reused.status_code == 400
    assert reused.json()["error"]["code"] == "auth.registration_closed"


def test_repeated_failed_logins_are_rate_limited(monkeypatch):
    # R2: after the configured number of failed attempts the limiter rejects
    # further tries with a 401 (anti-enumeration) using a distinct message.
    monkeypatch.setenv("CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS", "3")
    rate_limit.reset()
    client = TestClient(app)
    for _ in range(3):
        bad = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "wrong-password"},
        )
        assert bad.status_code == 401
        assert bad.json()["error"]["code"] == "auth.invalid_credentials"

    throttled = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "wrong-password"},
    )
    assert throttled.status_code == 401
    assert throttled.json()["error"]["code"] == "auth.invalid_credentials"
    # Even the CORRECT password is rejected while throttled.
    blocked_correct = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert blocked_correct.status_code == 401


def test_rotating_forwarded_for_cannot_bypass_login_throttle(monkeypatch):
    # R2 hardening: X-Forwarded-For is client-controllable, so by default it must
    # NOT be trusted for rate-limit bucketing. An attacker rotating the header per
    # request must still be throttled (the bucket keys on the real peer).
    monkeypatch.setenv("CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS", "3")
    monkeypatch.delenv("CUTAGENT_AUTH_TRUST_FORWARDED_FOR", raising=False)
    rate_limit.reset()
    client = TestClient(app)
    for attempt in range(3):
        bad = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "wrong-password"},
            headers={"X-Forwarded-For": f"10.0.0.{attempt}"},
        )
        assert bad.status_code == 401

    throttled = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "wrong-password"},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )
    assert throttled.status_code == 401
    # The correct password is also rejected while throttled, proving the limiter
    # engaged despite the rotating forwarded-for header.
    blocked_correct = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
        headers={"X-Forwarded-For": "10.0.0.123"},
    )
    assert blocked_correct.status_code == 401


def test_forwarded_for_is_honored_when_trust_enabled(monkeypatch):
    # When the deployment sits behind a trusted proxy, operators opt in via
    # CUTAGENT_AUTH_TRUST_FORWARDED_FOR=1, and distinct forwarded-for hops then
    # bucket independently (so a shared NAT does not lock everyone out).
    monkeypatch.setenv("CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS", "3")
    monkeypatch.setenv("CUTAGENT_AUTH_TRUST_FORWARDED_FOR", "1")
    rate_limit.reset()
    client = TestClient(app)
    for _attempt in range(3):
        bad = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "wrong-password"},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        assert bad.status_code == 401
    # A different forwarded-for hop is a different bucket: the wrong password is
    # still 401 (bad creds), but NOT throttled — a correct login succeeds.
    other_ip_ok = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
        headers={"X-Forwarded-For": "10.0.0.2"},
    )
    assert other_ip_ok.status_code == 200, other_ip_ok.text


def test_successful_login_clears_failure_counter(monkeypatch):
    # R2: a success resets the bucket so legitimate users are not locked out by
    # earlier typos.
    monkeypatch.setenv("CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS", "3")
    rate_limit.reset()
    client = TestClient(app)
    for _ in range(2):
        client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "wrong-password"},
        )
    good = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert good.status_code == 200, good.text
    # The bucket is cleared, so two more failures + a success still succeed.
    for _ in range(2):
        client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "wrong-password"},
        )
    again = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert again.status_code == 200, again.text


def test_registration_is_rate_limited(monkeypatch):
    # R2: registration attempts are throttled per client with a 400.
    monkeypatch.setenv("CUTAGENT_AUTH_MAX_REGISTRATION_ATTEMPTS", "2")
    rate_limit.reset()
    client = TestClient(app)
    body = {
        "password": "correct horse battery staple",
        "display_name": "RL User",
        "registration_code": "reg_local_admin",
    }
    for i in range(2):
        resp = client.post(
            "/api/auth/register",
            json={**body, "email": f"rl-{i}@example.test"},
        )
        assert resp.status_code == 201, resp.text
    throttled = client.post(
        "/api/auth/register",
        json={**body, "email": "rl-blocked@example.test"},
    )
    assert throttled.status_code == 400
    assert throttled.json()["error"]["code"] == "validation.invalid_options"


def test_register_rejects_weak_password():
    # R5: server-side password strength policy applies to registration.
    rate_limit.reset()
    client = TestClient(app)
    resp = client.post(
        "/api/auth/register",
        json={
            "email": "weakpass@example.test",
            "password": "password123",
            "display_name": "Weak Pass",
            "registration_code": "reg_local_admin",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation.invalid_options"


def test_cannot_disable_last_active_admin():
    # R4: the seeded single admin cannot be disabled — would leave zero admins.
    # Use a fresh app so the seeded admin state is isolated from other tests.
    rate_limit.reset()
    client = TestClient(create_app())
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text

    resp = client.patch("/api/auth/users/usr_admin", json={"status": "disabled"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "validation.conflict"

    demote = client.patch("/api/auth/users/usr_admin", json={"role": "viewer"})
    assert demote.status_code == 409
    assert demote.json()["error"]["code"] == "validation.conflict"


def test_last_admin_protection_allows_demotion_when_another_admin_exists():
    # R4: demotion is allowed once a second active admin exists.
    rate_limit.reset()
    client = TestClient(create_app())
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text

    created = client.post(
        "/api/auth/users",
        json={
            "email": "second-admin@example.test",
            "password": "correct horse battery staple",
            "display_name": "Second Admin",
            "role": "admin",
        },
    )
    assert created.status_code == 201, created.text
    second_id = created.json()["id"]

    # The seeded admin can now be disabled because a second admin remains.
    disabled = client.patch("/api/auth/users/usr_admin", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"

    # The second admin is now the LAST active admin and cannot be demoted.
    # (Authenticate as that admin since the seeded admin is disabled.)
    second_login = client.post(
        "/api/auth/login",
        json={"identifier": "Second Admin", "password": "correct horse battery staple"},
    )
    assert second_login.status_code == 200, second_login.text
    blocked = client.patch(f"/api/auth/users/{second_id}", json={"role": "viewer"})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "validation.conflict"


def test_change_password_revokes_other_sessions_keeps_caller():
    # R5: changing the password revokes OTHER sessions but keeps the caller's.
    rate_limit.reset()
    # Session A (will change the password) and session B (a second login) must
    # share ONE app instance so both sessions live in the same repository.
    shared_app = create_app()
    client_a = TestClient(shared_app)
    client_b = TestClient(shared_app)
    for client in (client_a, client_b):
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert login.status_code == 200, login.text

    # Both sessions are initially valid.
    assert client_a.get("/api/auth/session").status_code == 200
    assert client_b.get("/api/auth/session").status_code == 200

    changed = client_a.post(
        "/api/auth/me/change-password",
        json={"old_password": "local-admin", "new_password": "Str0ng-NewPass!"},
    )
    assert changed.status_code == 200, changed.text

    # Caller (A) keeps its session; the other session (B) is revoked.
    assert client_a.get("/api/auth/session").status_code == 200
    assert client_b.get("/api/auth/session").status_code == 401


def test_change_password_rejects_weak_new_password():
    # R5: strength policy also applies to change-password.
    rate_limit.reset()
    client = TestClient(create_app())
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text
    resp = client.post(
        "/api/auth/me/change-password",
        json={"old_password": "local-admin", "new_password": "password123"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation.invalid_options"


def test_session_cookie_is_httponly_and_samesite_lax_by_default():
    # Spec §33.2: the session cookie MUST be HttpOnly; SameSite=lax is also set.
    rate_limit.reset()
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    # Plain-HTTP TestClient with the knob unset must NOT mark the cookie Secure
    # (deriving from the http:// request scheme keeps local dev usable).
    assert "secure" not in cookie.lower()


def test_session_cookie_secure_forced_in_production(monkeypatch):
    # Spec §33.2: production MUST set Secure. The explicit knob forces it on even
    # though the TestClient request itself is plain HTTP.
    monkeypatch.setenv("CUTAGENT_AUTH_COOKIE_SECURE", "true")
    rate_limit.reset()
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie


def test_session_cookie_secure_derived_from_forwarded_proto(monkeypatch):
    # Behind a trusted TLS-terminating proxy: X-Forwarded-Proto=https marks the
    # cookie Secure when forwarded headers are trusted, with the knob left unset.
    monkeypatch.setenv("CUTAGENT_AUTH_TRUST_FORWARDED_FOR", "true")
    monkeypatch.delenv("CUTAGENT_AUTH_COOKIE_SECURE", raising=False)
    rate_limit.reset()
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 200, response.text
    assert "Secure" in response.headers["set-cookie"]


def test_logout_clears_cookie_with_secure_when_forced(monkeypatch):
    monkeypatch.setenv("CUTAGENT_AUTH_COOKIE_SECURE", "true")
    rate_limit.reset()
    # Use an https base_url so the Secure session cookie round-trips back on the
    # authenticated logout call (a Secure cookie is not sent over plain HTTP).
    client = TestClient(app, base_url="https://testserver")
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text
    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200, logout.text
    cookie = logout.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
