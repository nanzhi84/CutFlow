
from fastapi.testclient import TestClient


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from uuid import uuid4

from packages.core.storage.database import (
    PerformanceObservationRow,
    PerformanceScoreRow,
    PublishRecordRow,
    ScriptDraftRow,
    ScriptVersionRow,
)
from packages.core.contracts import utcnow


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    return session_factory


def test_sqlalchemy_case_agent_learning_boundary_is_persisted():
    session_factory = sqlalchemy_session_factory()

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        generated = client.post(
            "/api/cases/case_demo/scripts/generate-with-memory",
            json={
                "brief": "Make this feel specific.",
                "memory_ids": [],
                "persona_mode": "hard_ad",
                "operation": "generate",
                "variation_count": 1,
            },
        )
        assert generated.status_code == 202, generated.text

        drafts = client.get("/api/cases/case_demo/agent/drafts")
        assert drafts.status_code == 200, drafts.text
        draft = next(item for item in drafts.json()["items"] if item["id"] == generated.json()["id"])

        adopted = client.post(
            f"/api/cases/case_demo/agent/drafts/{draft['id']}/adopt",
            json={"title": "Adopted proof-first script", "publish_content": "Proof first. Then offer."},
        )
        assert adopted.status_code == 201, adopted.text
        script_version = adopted.json()
        assert script_version["adopted_from_draft_id"] == draft["id"]

        rubric = client.get("/api/cases/case_demo/rubric")
        assert rubric.status_code == 200, rubric.text
        assert rubric.json()["status"] == "active"

    with session_factory() as session:
        assert session.get(ScriptVersionRow, script_version["id"]) is not None
        assert session.get(ScriptDraftRow, generated.json()["id"]) is not None


def test_sqlalchemy_metrics_import_matched_row_persists_observation_and_score():
    """DB-path metrics import must not raise on the happy (matched) path.

    Regression for the blocker where import_metrics scored a contract obtained
    from an *unflushed* ORM row (None created_at/updated_at/schema_version),
    raising a pydantic ValidationError -> HTTP 500 on every matched row.
    """
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:8]
    publish_record_id = f"pub_metrics_{suffix}"

    with session_factory() as session:
        session.add(
            PublishRecordRow(
                id=publish_record_id,
                case_id="case_demo",
                platform="douyin",
                status="published",
                published_at=utcnow(),
            )
        )
        session.commit()

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        # Bind by the operator-supplied publish_record_id (deterministic, not a
        # title/time guess) so the row matches regardless of matching_policy.
        response = client.post(
            "/api/cases/case_demo/metrics/import",
            json={
                "rows": [
                    {
                        "publish_record_id": publish_record_id,
                        "impressions": 50000,
                        "views": 12000,
                        "completion_rate": 0.42,
                        "metric_name": "completion_rate",
                        "metric_value": 0.42,
                        "window": "7d",
                    }
                ],
                "matching_policy": "strict_manual",
            },
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["created_count"] == 1
        assert body["skipped_count"] == 0
        observation_id = body["results"][0]["internal_id"]

    with session_factory() as session:
        obs_row = session.get(PerformanceObservationRow, observation_id)
        assert obs_row is not None
        assert obs_row.publish_record_id == publish_record_id
        assert obs_row.impressions == 50000
        assert obs_row.created_at is not None
        score_rows = list(
            session.query(PerformanceScoreRow).filter(
                PerformanceScoreRow.observation_id == observation_id
            )
        )
        assert len(score_rows) == 1
        assert score_rows[0].normalized_score == 0.42
