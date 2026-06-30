
import pytest
from uuid import uuid4
from fastapi.testclient import TestClient


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from packages.core.storage.database import CaseRow, IdempotencyRecordRow
from packages.creative.cases import SqlAlchemyCaseRepository


def test_sqlalchemy_case_repository_reads_seeded_case():
    session_factory = get_sqlalchemy_session_factory()
    assert session_factory is not None
    repository = SqlAlchemyCaseRepository(session_factory)
    cases = repository.list_cases(limit=200)
    assert any(case.id == "case_demo" for case in cases)
    assert repository.get_case("case_demo").name == "Demo Case"


def test_cases_api_reads_from_sqlalchemy_backend():
    with TestClient(app) as client:
        viewer_login = client.post(
            "/api/auth/login",
            json={"email": "viewer@local.cutagent", "password": "local-viewer"},
        )
        assert viewer_login.status_code == 200, viewer_login.text

        response = client.get("/api/cases", params={"limit": 200})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert any(item["id"] == "case_demo" for item in items)


def test_cases_api_persists_created_and_patched_case():
    session_factory = get_sqlalchemy_session_factory()
    assert session_factory is not None

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        created = client.post(
            "/api/cases",
            json={
                "name": "Integration Case",
                "description": "Created through the SQLAlchemy API path",
                "industry": "education",
                "product": "course",
                "target_audience": "creators",
                "key_selling_points": ["fast", "cheap"],
                "ip_persona": "friendly expert",
                "brand_voice": "warm and direct",
                "strategy_tags": ["promo", "q3"],
                "brand_keywords": ["acme"],
                "competitor_names": ["globex"],
            },
        )
        assert created.status_code == 201, created.text
        case = created.json()
        assert case["key_selling_points"] == ["fast", "cheap"]
        assert case["ip_persona"] == "friendly expert"
        assert case["brand_voice"] == "warm and direct"
        assert case["strategy_tags"] == ["promo", "q3"]
        assert case["brand_keywords"] == ["acme"]
        assert case["competitor_names"] == ["globex"]

        patched = client.patch(
            f"/api/cases/{case['id']}",
            json={
                "product": "membership",
                "target_audience": "creator operators",
                "industry": "edtech",
                "key_selling_points": ["personalized"],
                "ip_persona": "mentor",
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["product"] == "membership"
        assert patched.json()["industry"] == "edtech"
        assert patched.json()["key_selling_points"] == ["personalized"]
        assert patched.json()["ip_persona"] == "mentor"

        industry_filtered = client.get("/api/cases", params={"industry": "edtech"})
        assert industry_filtered.status_code == 200, industry_filtered.text
        assert any(item["id"] == case["id"] for item in industry_filtered.json()["items"])
        for item in industry_filtered.json()["items"]:
            assert {"material_count", "script_count", "voice_count", "quality_count"} <= set(item)

        filtered = client.get(
            "/api/cases",
            params={"search": "Integration", "owner_user_id": "usr_admin"},
        )
        assert filtered.status_code == 200, filtered.text
        assert any(item["id"] == case["id"] for item in filtered.json()["items"])

        other_owner = client.get("/api/cases", params={"owner_user_id": "usr_viewer"})
        assert other_owner.status_code == 200, other_owner.text
        assert all(item["id"] != case["id"] for item in other_owner.json()["items"])

    with session_factory() as session:
        row = session.get(CaseRow, case["id"])
        assert row is not None
        assert row.owner_user_id == "usr_admin"
        assert row.product == "membership"
        assert row.target_audience == "creator operators"
        assert row.industry == "edtech"
        assert list(row.key_selling_points) == ["personalized"]
        assert row.ip_persona == "mentor"
        assert row.brand_voice == "warm and direct"
        assert list(row.strategy_tags) == ["promo", "q3"]
        assert list(row.brand_keywords) == ["acme"]
        assert list(row.competitor_names) == ["globex"]


def test_sqlalchemy_idempotency_replays_after_app_reconfiguration():
    session_factory = get_sqlalchemy_session_factory()
    assert session_factory is not None

    idem_key = f"case-create-after-restart-{uuid4().hex[:8]}"
    headers = {"Idempotency-Key": idem_key}
    payload = {"name": "Idempotent SQL Case"}
    with TestClient(app) as first_client:
        login = first_client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert login.status_code == 200, login.text
        first = first_client.post("/api/cases", json=payload, headers=headers)
        assert first.status_code == 201, first.text
        first_case = first.json()

    with TestClient(app) as second_client:
        login = second_client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert login.status_code == 200, login.text
        replayed = second_client.post("/api/cases", json=payload, headers=headers)
        assert replayed.status_code == 200, replayed.text
        assert replayed.headers["Idempotency-Replayed"] == "true"
        assert replayed.json()["id"] == first_case["id"]

        conflict = second_client.post(
            "/api/cases",
            json={"name": "Different SQL Case"},
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency.conflict"

    with session_factory() as session:
        record = session.get(
            IdempotencyRecordRow,
            (f"usr_admin:{idem_key}", "POST", "/api/cases"),
        )
        assert record is not None
        assert record.response_body["id"] == first_case["id"]
