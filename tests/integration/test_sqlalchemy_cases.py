import os

import pytest
from fastapi.testclient import TestClient

if os.getenv("CUTAGENT_RUN_DB_TESTS") != "1":
    pytest.skip("Set CUTAGENT_RUN_DB_TESTS=1 to run database integration tests.", allow_module_level=True)

from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import CaseRow, IdempotencyRecordRow
from packages.creative.cases import SqlAlchemyCaseRepository


def test_sqlalchemy_case_repository_reads_seeded_case():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
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
    session_factory = get_sqlalchemy_session_factory_if_enabled()
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
            },
        )
        assert created.status_code == 201, created.text
        case = created.json()

        patched = client.patch(
            f"/api/cases/{case['id']}",
            json={
                "product": "membership",
                "target_audience": "creator operators",
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["product"] == "membership"

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


def test_sqlalchemy_idempotency_replays_after_app_reconfiguration():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    assert session_factory is not None

    headers = {"Idempotency-Key": "case-create-after-restart"}
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
            ("usr_admin:case-create-after-restart", "POST", "/api/cases"),
        )
        assert record is not None
        assert record.response_body["id"] == first_case["id"]
