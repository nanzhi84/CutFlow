"""Reference-extractor cookie endpoints (import / test / status / refresh)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.creative import reference_cookies


def _login(client: TestClient, email: str = "admin@local.cutagent", password: str = "local-admin") -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text


def _clear_cookies(app) -> None:
    """Cookies persist via the file-backed SecretStore; isolate each test."""
    store = app.state.secret_store
    store.disable(reference_cookies.DOUYIN_COOKIE_SECRET_REF)
    store.disable(reference_cookies.DOUYIN_COOKIE_META_SECRET_REF)


def test_import_then_status_reports_cookie_presence() -> None:
    app = create_app()
    with TestClient(app) as client:
        _clear_cookies(app)
        _login(client)
        pre = client.get("/api/creative/reference-extractor/status")
        assert pre.status_code == 200, pre.text
        assert pre.json()["cookie"]["cookie_present"] is False
        assert pre.json()["auto_refresh_supported"] is False

        imported = client.post(
            "/api/creative/reference-extractor/import-cookies",
            json={"cookie_text": "sessionid=abc; ttwid=xyz", "format": "auto", "source": "paste"},
        )
        assert imported.status_code == 200, imported.text
        body = imported.json()
        assert body["success"] is True
        assert body["status"]["cookie_count"] == 2
        assert body["status"]["source"] == "paste"

        post = client.get("/api/creative/reference-extractor/status")
        assert post.json()["cookie"]["cookie_present"] is True
        assert post.json()["cookie"]["cookie_count"] == 2


def test_import_rejects_unrecognised_cookie_blob() -> None:
    app = create_app()
    with TestClient(app) as client:
        _clear_cookies(app)
        _login(client)
        response = client.post(
            "/api/creative/reference-extractor/import-cookies",
            json={"cookie_text": "this is not a cookie", "format": "json"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "reference.cookie_invalid"


def test_import_requires_operator_role() -> None:
    app = create_app()
    with TestClient(app) as client:
        _clear_cookies(app)
        _login(client, "viewer@local.cutagent", "local-viewer")
        response = client.post(
            "/api/creative/reference-extractor/import-cookies",
            json={"cookie_text": "sessionid=abc", "format": "auto"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "auth.forbidden"


def test_test_cookies_with_url_uses_mocked_metadata_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str | None] = {}

    async def fake_fetch_metadata(url: str, *, cookie_header: str | None = None) -> dict[str, str]:
        captured["url"] = url
        captured["cookie"] = cookie_header
        return {"title": "Mocked Title", "platform": "douyin"}

    # Patch the yt-dlp metadata fetch so no network call happens.
    monkeypatch.setattr(
        "packages.creative.reference_extract.fetch_metadata",
        fake_fetch_metadata,
    )

    app = create_app()
    with TestClient(app) as client:
        _clear_cookies(app)
        _login(client)
        client.post(
            "/api/creative/reference-extractor/import-cookies",
            json={"cookie_text": "sessionid=abc; ttwid=xyz", "format": "auto"},
        )
        response = client.post(
            "/api/creative/reference-extractor/test-cookies",
            json={"url": "https://www.douyin.com/video/123"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert body["title"] == "Mocked Title"
        assert body["test_url"] == "https://www.douyin.com/video/123"

    assert captured["url"] == "https://www.douyin.com/video/123"
    assert captured["cookie"] == "sessionid=abc; ttwid=xyz"


def test_test_cookies_without_stored_cookies_fails_gracefully() -> None:
    app = create_app()
    with TestClient(app) as client:
        _clear_cookies(app)
        _login(client)
        response = client.post("/api/creative/reference-extractor/test-cookies", json={})
        assert response.status_code == 200, response.text
        assert response.json()["success"] is False


def test_refresh_cookies_returns_410_not_supported() -> None:
    app = create_app()
    with TestClient(app) as client:
        _clear_cookies(app)
        _login(client)
        response = client.post("/api/creative/reference-extractor/refresh-cookies")
        assert response.status_code == 410, response.text
        assert response.json()["error"]["code"] == "reference.refresh_not_supported"
