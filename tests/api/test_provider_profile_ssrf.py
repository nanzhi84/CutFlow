"""SSRF guard on the provider-profile create/patch API (spec §33.2 hardening).

base_url is user-settable via default_options and the stored bearer secret is
later delivered to it, so an off-list host must be rejected before persist.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core.auth import rate_limit


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limit.reset()
    yield
    rate_limit.reset()


def _admin_client() -> TestClient:
    # Fresh app per test: creating provider profiles must not pollute the shared
    # module-level singleton repository that other suites (e.g. golden) reuse.
    client = TestClient(create_app())
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text
    return client


def _profile_payload(base_url: str) -> dict:
    return {
        "provider_id": "dashscope.llm",
        "model_id": "qwen3.7-plus",
        "capability": "llm.chat",
        "display_name": "Test profile",
        "environment": "prod",
        "options_schema_ref": {"schema_id": "provider.llm.options"},
        "default_options": {"base_url": base_url},
    }


def test_create_profile_rejects_offlist_base_url():
    client = _admin_client()
    response = client.post(
        "/api/providers/profiles",
        json=_profile_payload("https://evil.example.com/v1"),
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "validation.invalid_options"
    assert "not allowed" in body["error"]["message"]


def test_create_profile_allows_sanctioned_base_url():
    client = _admin_client()
    response = client.post(
        "/api/providers/profiles",
        json=_profile_payload("https://dashscope.aliyuncs.com/api/v1"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["default_options"]["base_url"] == "https://dashscope.aliyuncs.com/api/v1"


def test_patch_profile_rejects_offlist_base_url():
    client = _admin_client()
    created = client.post(
        "/api/providers/profiles",
        json=_profile_payload("https://dashscope.aliyuncs.com/api/v1"),
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    patched = client.patch(
        f"/api/providers/profiles/{profile_id}",
        json={"default_options": {"base_url": "http://169.254.169.254/latest/meta-data"}},
    )
    assert patched.status_code == 400, patched.text
    assert patched.json()["error"]["code"] == "validation.invalid_options"


def test_patch_without_default_options_is_unaffected():
    client = _admin_client()
    created = client.post(
        "/api/providers/profiles",
        json=_profile_payload("https://dashscope.aliyuncs.com/api/v1"),
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    patched = client.patch(
        f"/api/providers/profiles/{profile_id}",
        json={"display_name": "Renamed"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == "Renamed"
