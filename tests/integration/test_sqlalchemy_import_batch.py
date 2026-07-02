from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from packages.core.storage.database import (
    CaseRow,
    MediaAssetRow,
    PromptTemplateRow,
    PromptVersionRow,
    ProviderPriceCatalogRow,
    ProviderPriceItemRow,
    ScriptVersionRow,
)


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def import_one(client: TestClient, import_type: str, row: dict) -> tuple[str, str]:
    response = client.post("/api/import/batches", json={"import_type": import_type, "rows": [row]})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created_count"] == 1
    return body["batch_id"], body["results"][0]["internal_id"]


def test_sqlalchemy_import_batch_remaining_types_are_persisted_and_listed():
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:8]

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        case_batch_id, case_id = import_one(
            client,
            "case",
            {
                "name": f"Imported Case {suffix}",
                "description": "Imported through SQLAlchemy batch",
                "industry": "education",
            },
        )
        _, script_id = import_one(
            client,
            "script",
            {
                "case_id": "case_demo",
                "title": f"Imported Script {suffix}",
                "script": "Imported script body.",
            },
        )
        _, media_id = import_one(
            client,
            "media",
            {
                "case_id": "case_demo",
                "title": f"Imported Media {suffix}",
                "kind": "video",
                "uri": f"s3://cutagent-durable/imports/{suffix}/broll.mp4",
                "tags": ["imported", "sqlalchemy"],
            },
        )
        _, prompt_id = import_one(
            client,
            "prompt_seed",
            {
                "name": f"Imported Prompt {suffix}",
                "purpose": "import.seed",
                "content": "Return a concise hook.",
            },
        )
        _, price_catalog_id = import_one(
            client,
            "provider_price",
            {
                "provider_id": f"sandbox-import-{suffix}",
                "currency": "CNY",
                "unit_price": {"currency": "CNY", "amount": 0.25},
            },
        )

        cases = client.get("/api/cases")
        assert cases.status_code == 200, cases.text
        assert any(item["id"] == case_id for item in cases.json()["items"])

        with session_factory() as session:
            assert session.get(ScriptVersionRow, script_id) is not None

        media = client.get("/api/media/assets", params={"case_id": "case_demo", "kind": "video"})
        assert media.status_code == 200, media.text
        assert any(item["asset"]["id"] == media_id for item in media.json()["items"])

        prompts = client.get("/api/prompts")
        assert prompts.status_code == 200, prompts.text
        assert any(item["template"]["id"] == prompt_id for item in prompts.json()["items"])

        catalogs = client.get("/api/providers/price-catalogs")
        assert catalogs.status_code == 200, catalogs.text
        assert any(item["id"] == price_catalog_id for item in catalogs.json()["items"])

        stored_report = client.get(f"/api/import/batches/{case_batch_id}")
        assert stored_report.status_code == 200, stored_report.text
        assert stored_report.json()["batch_id"] == case_batch_id

    with session_factory() as session:
        assert session.get(CaseRow, case_id) is not None
        assert session.get(ScriptVersionRow, script_id) is not None
        assert session.get(MediaAssetRow, media_id) is not None
        assert session.get(PromptTemplateRow, prompt_id) is not None
        prompt_version = session.query(PromptVersionRow).filter_by(prompt_template_id=prompt_id).one_or_none()
        assert prompt_version is not None
        assert prompt_version.status == "published"
        assert session.get(ProviderPriceCatalogRow, price_catalog_id) is not None
        price_item = session.query(ProviderPriceItemRow).filter_by(catalog_id=price_catalog_id).one_or_none()
        assert price_item is not None
        assert price_item.unit_price == {"currency": "CNY", "amount": 0.25}
