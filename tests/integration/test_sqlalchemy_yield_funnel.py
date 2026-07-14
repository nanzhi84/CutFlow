"""SQL/Temporal-backend yield-funnel emission (spec §9.5).

These guard the production backend: the publish / quality-check / approval
lifecycle stages (``published`` / ``qc_*`` / ``manual_*``) must be persisted to
``yield_funnel_events`` by the SQL-backed repositories, not only by the in-memory
API fallback. Without them ``true_yield_rate`` collapses to 0.0 in production
even for fully published runs (the central deliverable of the funnel workstream).
"""

from uuid import uuid4

from fastapi.testclient import TestClient


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    return session_factory


def _login_admin(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert resp.status_code == 200, resp.text


def _create_run_with_finished_video(client: TestClient) -> tuple[str, str]:
    """Drive a real digital-human-video job to completion and return (run_id, finished_video_id)."""
    created = client.post(
        "/api/jobs/digital-human-video",
        json={
            "case_id": "case_demo",
            "title": f"Yield funnel SQL run {uuid4().hex[:6]}",
            "script": "用一个简短脚本验证成品率漏斗在数据库后端的事件落库。",
            "publish_content": "Yield funnel DB handoff.",
            "voice": {"voice_id": "voice_sandbox"},
            "strictness": {"strict_timestamps": False},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    run_id = body["initial_run"]["id"]
    assert body["initial_run"]["status"] in {"succeeded", "degraded"}, body

    listed = client.get("/api/cases/case_demo/finished-videos", params={"limit": 200})
    assert listed.status_code == 200, listed.text
    finished = next((v for v in listed.json()["items"] if v.get("run_id") == run_id), None)
    assert finished is not None, f"no finished video produced for run {run_id}"
    return run_id, finished["id"]


def _funnel_events(client: TestClient, run_id: str) -> set[str]:
    funnel = client.get("/api/ops/yield-funnel", params={"case_id": "case_demo"})
    assert funnel.status_code == 200, funnel.text
    return {e["event_type"] for e in funnel.json()["events"] if e.get("run_id") == run_id}


def test_sqlalchemy_publish_emits_published_event_and_nonzero_true_yield():
    sqlalchemy_session_factory()
    with TestClient(app) as client:
        _login_admin(client)
        run_id, finished_video_id = _create_run_with_finished_video(client)

        package = client.post(
            "/api/publish/packages",
            json={
                "source_finished_video_id": finished_video_id,
                "title": "Yield funnel publish package",
                "description": "Drives the published funnel stage on the SQL backend.",
            },
        )
        assert package.status_code == 201, package.text
        package_id = package.json()["id"]

        batch = client.post(
            "/api/publish/batches",
            json={"publish_package_ids": [package_id], "platform_targets": ["douyin"]},
        )
        assert batch.status_code == 201, batch.text
        batch_id = batch.json()["id"]

        submitted = client.post(f"/api/publish/batches/{batch_id}/submit", json={"dry_run": False})
        assert submitted.status_code == 202, submitted.text

        # The SQL publishing repo must emit the §9.5 publish stages, run-linked.
        kinds = _funnel_events(client, run_id)
        assert "publish_started" in kinds, kinds
        assert "published" in kinds, kinds

        funnel = client.get("/api/ops/yield-funnel", params={"case_id": "case_demo"})
        rate = funnel.json()["true_yield_rate"]
        assert rate is not None and rate > 0.0, f"true_yield_rate should be >0 once a run is published, got {rate}"


def test_sqlalchemy_quality_check_and_approval_emit_funnel_stages():
    sqlalchemy_session_factory()
    with TestClient(app) as client:
        _login_admin(client)
        run_id, _ = _create_run_with_finished_video(client)

        qc = client.post(
            f"/api/runs/{run_id}/quality-checks",
            json={"check_type": "auto", "result": "failed", "reason_code": "integration"},
        )
        assert qc.status_code == 201, qc.text

        # An approval whose id == run_id links the manual_* stage back to the run.
        rejected = client.post(
            f"/api/approval-requests/{run_id}/reject",
            json={"reason": "quality check failed"},
        )
        assert rejected.status_code == 200, rejected.text

        kinds = _funnel_events(client, run_id)
        assert "qc_started" in kinds, kinds
        assert "qc_failed" in kinds, kinds
        assert "manual_rejected" in kinds, kinds
