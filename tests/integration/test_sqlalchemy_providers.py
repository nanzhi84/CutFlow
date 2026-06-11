import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

if os.getenv("CUTAGENT_RUN_DB_TESTS") != "1":
    pytest.skip("Set CUTAGENT_RUN_DB_TESTS=1 to run database integration tests.", allow_module_level=True)

from apps.api.main import app
from packages.ai.gateway import ProviderCall, ProviderGateway
from packages.ai.gateway.sqlalchemy_repository import SqlAlchemyProviderRuntimeRepository
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import ProviderPriceCatalogRow, ProviderPriceItemRow, ProviderProfileRow
from packages.core.storage.repository import Repository


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def test_sqlalchemy_provider_configuration_and_price_catalog_flow_is_persisted():
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:8]
    catalog_id = f"price_catalog_{suffix}"
    item_id = f"price_item_{suffix}"

    with TestClient(app) as client:
        viewer_login = client.post(
            "/api/auth/login",
            json={"email": "viewer@local.cutagent", "password": "local-viewer"},
        )
        assert viewer_login.status_code == 200, viewer_login.text
        forbidden = client.post(
            "/api/providers/profiles",
            json={
                "provider_id": f"forbidden-{suffix}",
                "model_id": "local-model",
                "capability": "text.generate",
                "display_name": "Forbidden Provider",
                "environment": "local",
                "options_schema_ref": {"schema_id": "provider.options", "schema_version": "v1"},
                "default_options": {},
            },
        )
        assert forbidden.status_code == 403

        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        created = client.post(
            "/api/providers/profiles",
            json={
                "provider_id": f"sandbox-{suffix}",
                "model_id": "local-model",
                "capability": "text.generate",
                "display_name": "Integration Provider",
                "environment": "local",
                "options_schema_ref": {"schema_id": "provider.options", "schema_version": "v1"},
                "default_options": {"temperature": 0.2},
            },
        )
        assert created.status_code == 201, created.text
        profile = created.json()

        runtime_created = client.post(
            "/api/providers/profiles",
            json={
                "provider_id": "sandbox",
                "model_id": f"tts-{suffix}",
                "capability": "tts.speech",
                "display_name": "Runtime Visible TTS",
                "environment": "local",
                "options_schema_ref": {"schema_id": "provider.tts.options", "schema_version": "v1"},
                "default_options": {},
            },
        )
        assert runtime_created.status_code == 201, runtime_created.text
        runtime_profile = runtime_created.json()

        runtime_gateway = ProviderGateway(
            Repository(),
            provider_reader=SqlAlchemyProviderRuntimeRepository(session_factory),
        )
        invocation, result = runtime_gateway.invoke(
            ProviderCall(
                provider_profile_id=runtime_profile["id"],
                capability_id="tts.speech",
                input={"text": "runtime profile is visible"},
            )
        )
        assert invocation.status == "succeeded"
        assert result is not None
        assert result.output["audio_uri"].startswith("sandbox://audio/")

        health = client.post(f"/api/providers/profiles/{profile['id']}/test", json={"sample_input": {}})
        assert health.status_code == 200, health.text
        assert health.json()["ok"] is True

        patched = client.patch(
            f"/api/providers/profiles/{profile['id']}",
            json={"display_name": "Patched Provider", "enabled": False, "default_options": {"temperature": 0.6}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["enabled"] is False

        listed_profiles = client.get("/api/providers/profiles")
        assert listed_profiles.status_code == 200, listed_profiles.text
        assert any(item["id"] == profile["id"] for item in listed_profiles.json()["items"])

        filtered_profiles = client.get(
            "/api/providers/profiles",
            params={
                "provider_id": profile["provider_id"],
                "capability": profile["capability"],
                "environment": profile["environment"],
            },
        )
        assert filtered_profiles.status_code == 200, filtered_profiles.text
        assert any(item["id"] == profile["id"] for item in filtered_profiles.json()["items"])
        assert all(item["provider_id"] == profile["provider_id"] for item in filtered_profiles.json()["items"])

        capabilities = client.get("/api/providers/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        assert len(capabilities.json()) > 0

        balances = client.get("/api/providers/balances", params={"provider_id": profile["provider_id"]})
        assert balances.status_code == 200, balances.text
        assert balances.json()["items"][0]["provider_id"] == profile["provider_id"]

        upserted = client.post(
            "/api/providers/price-catalogs",
            json={
                "catalog": {
                    "id": catalog_id,
                    "provider_id": profile["provider_id"],
                    "status": "draft",
                    "currency": "CNY",
                },
                "items": [
                    {
                        "id": item_id,
                        "catalog_id": catalog_id,
                        "provider_id": profile["provider_id"],
                        "model_id": profile["model_id"],
                        "capability_id": profile["capability"],
                        "unit": "call",
                        "unit_price": {"currency": "CNY", "amount": 0.5},
                    }
                ],
            },
        )
        assert upserted.status_code == 201, upserted.text
        assert upserted.json()["status"] == "draft"

        approved = client.post(
            f"/api/providers/price-catalogs/{catalog_id}/approve",
            json={"reason": "integration approve"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"

        published = client.post(
            f"/api/providers/price-catalogs/{catalog_id}/publish",
            json={"reason": "integration publish"},
        )
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "published"

        active_catalogs = client.get(
            "/api/providers/price-catalogs",
            params={"provider_id": profile["provider_id"], "active_only": True},
        )
        assert active_catalogs.status_code == 200, active_catalogs.text
        assert any(item["id"] == catalog_id for item in active_catalogs.json()["items"])
        assert all(item["status"] == "published" for item in active_catalogs.json()["items"])

        deprecated = client.post(
            f"/api/providers/price-catalogs/{catalog_id}/deprecate",
            json={"reason": "integration deprecate"},
        )
        assert deprecated.status_code == 200, deprecated.text
        assert deprecated.json()["status"] == "deprecated"

        listed_catalogs = client.get("/api/providers/price-catalogs")
        assert listed_catalogs.status_code == 200, listed_catalogs.text
        assert any(item["id"] == catalog_id for item in listed_catalogs.json()["items"])

    with session_factory() as session:
        profile_row = session.get(ProviderProfileRow, profile["id"])
        catalog_row = session.get(ProviderPriceCatalogRow, catalog_id)
        item_row = session.get(ProviderPriceItemRow, item_id)
        assert profile_row is not None
        assert profile_row.display_name == "Patched Provider"
        assert profile_row.enabled is False
        assert profile_row.default_options == {"temperature": 0.6}
        assert catalog_row is not None
        assert catalog_row.status == "deprecated"
        assert item_row is not None
        assert item_row.unit_price == {"currency": "CNY", "amount": "0.5", "amount_micro": 500000}
