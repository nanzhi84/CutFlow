import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core.auth import rate_limit


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limit.reset()
    yield
    rate_limit.reset()


def _login(client: TestClient, email: str, password: str) -> None:
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text


def test_unset_defaults_returns_system_defaults():
    client = TestClient(create_app())
    _login(client, "admin@local.cutagent", "local-admin")
    resp = client.get("/api/auth/me/generation-defaults")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No record yet -> all blocks are None (caller falls back to system defaults).
    assert body == {
        "voice": None,
        "portrait": None,
        "broll": None,
        "lipsync": None,
        "subtitle": None,
        "bgm": None,
        "cover": None,
        "output": None,
        "strictness": None,
    }


def test_put_then_get_round_trips():
    client = TestClient(create_app())
    _login(client, "admin@local.cutagent", "local-admin")
    payload = {
        "voice": {"voice_id": "vc_custom", "speed": 1.25, "emotion": "happy"},
        "output": {"width": 720, "height": 1280, "fps": 24},
    }
    put = client.put("/api/auth/me/generation-defaults", json=payload)
    assert put.status_code == 200, put.text
    saved = put.json()
    assert saved["voice"]["voice_id"] == "vc_custom"
    assert saved["voice"]["speed"] == 1.25
    assert saved["output"]["width"] == 720
    # Unset blocks stay None.
    assert saved["portrait"] is None

    again = client.get("/api/auth/me/generation-defaults")
    assert again.status_code == 200, again.text
    assert again.json() == saved


def test_put_is_upsert_and_replaces_previous():
    client = TestClient(create_app())
    _login(client, "admin@local.cutagent", "local-admin")
    client.put(
        "/api/auth/me/generation-defaults",
        json={"voice": {"voice_id": "vc_first"}},
    )
    second = client.put(
        "/api/auth/me/generation-defaults",
        json={"output": {"fps": 60}},
    )
    assert second.status_code == 200, second.text
    body = second.json()
    # The second PUT replaces the stored value: voice block is gone.
    assert body["voice"] is None
    assert body["output"]["fps"] == 60


def test_defaults_persist_across_sessions_for_same_user():
    app = create_app()
    client_a = TestClient(app)
    _login(client_a, "admin@local.cutagent", "local-admin")
    client_a.put(
        "/api/auth/me/generation-defaults",
        json={"voice": {"voice_id": "vc_persist"}},
    )

    # A brand-new session (same user, same app/repository) still sees it.
    client_b = TestClient(app)
    _login(client_b, "admin@local.cutagent", "local-admin")
    resp = client_b.get("/api/auth/me/generation-defaults")
    assert resp.status_code == 200, resp.text
    assert resp.json()["voice"]["voice_id"] == "vc_persist"


def test_defaults_are_isolated_per_user():
    app = create_app()
    admin = TestClient(app)
    _login(admin, "admin@local.cutagent", "local-admin")
    admin.put(
        "/api/auth/me/generation-defaults",
        json={"voice": {"voice_id": "vc_admin"}},
    )

    viewer = TestClient(app)
    _login(viewer, "viewer@local.cutagent", "local-viewer")
    # The viewer has not saved anything: must NOT see the admin's defaults.
    resp = viewer.get("/api/auth/me/generation-defaults")
    assert resp.status_code == 200, resp.text
    assert resp.json()["voice"] is None


def test_generation_defaults_require_authentication():
    client = TestClient(create_app())
    resp = client.get("/api/auth/me/generation-defaults")
    assert resp.status_code == 401
