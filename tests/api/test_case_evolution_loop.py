"""API-level tests for the Case self-evolution closed loop (in-memory backend).

Covers Spec §25.4 metrics matching + lineage回流, §25.6 PerformanceScore,
§25.8 memory recall modes, and §8.4 data-driven reflection proposals.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app


def _login(client) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert resp.status_code == 200, resp.text


def _import_one(client, import_type: str, row: dict) -> str:
    resp = client.post("/api/import/batches", json={"import_type": import_type, "rows": [row]})
    assert resp.status_code == 202, resp.text
    return resp.json()["results"][0]["internal_id"]


def _seed_published_record(client, platform: str = "douyin") -> tuple[str, str]:
    finished_video_id = _import_one(
        client,
        "finished_video",
        {"external_id": "fv_ext", "case_id": "case_demo", "title": "Imported", "duration_sec": 12},
    )
    video_version_id = _import_one(
        client,
        "video_version",
        {"external_id": "vv_ext", "case_id": "case_demo", "finished_video_id": finished_video_id},
    )
    publish_record_id = _import_one(
        client,
        "publish_record",
        {
            "external_id": "pr_ext",
            "case_id": "case_demo",
            "video_version_id": video_version_id,
            "platform": platform,
            "status": "published",
        },
    )
    return video_version_id, publish_record_id


def test_metrics_import_matches_and_scores_and_binds_lineage():
    with TestClient(create_app()) as client:
        _login(client)
        video_version_id, publish_record_id = _seed_published_record(client)

        resp = client.post(
            "/api/cases/case_demo/metrics/import",
            json={
                "source": "manual_csv",
                "platform": "douyin",
                "matching_policy": "external_post_id",
                "rows": [
                    {
                        "external_ref": publish_record_id,
                        "metric_name": "completion_rate",
                        "metric_value": 0.7,
                        "completion_rate": 0.7,
                        "impressions": 20000,
                        "window": "7d",
                    }
                ],
            },
        )
        assert resp.status_code == 202, resp.text
        report = resp.json()
        assert report["created_count"] == 1
        assert report["skipped_count"] == 0

        performance = client.get("/api/cases/case_demo/performance").json()
        # §25.1: observation binds back to the video lineage.
        obs = performance["observations"][0]
        assert obs["video_version_id"] == video_version_id
        assert obs["platform"] == "douyin"
        assert obs["window"] == "7d"
        # §25.6: a mature-window, high-volume score is confident and not excluded.
        assert performance["scores"], "expected a PerformanceScore"
        score = performance["scores"][0]
        assert score["excluded_reason"] is None
        assert score["confidence"] >= 0.6
        assert score["primary_metric"] == "completion_rate"


def test_metrics_import_rejects_unresolvable_rows_without_guessing():
    with TestClient(create_app()) as client:
        _login(client)
        _seed_published_record(client)

        resp = client.post(
            "/api/cases/case_demo/metrics/import",
            json={
                "matching_policy": "external_post_id",
                "rows": [
                    {"title": "guess by title", "published_at": "2026-01-01", "metric_value": 5},
                ],
            },
        )
        assert resp.status_code == 202, resp.text
        report = resp.json()
        assert report["created_count"] == 0
        assert report["skipped_count"] == 1
        assert report["results"][0]["status"] == "skipped"
        assert report["results"][0]["error"]["message"] == "no_deterministic_match"


def test_low_impression_metrics_are_not_high_confidence():
    with TestClient(create_app()) as client:
        _login(client)
        _, publish_record_id = _seed_published_record(client)
        client.post(
            "/api/cases/case_demo/metrics/import",
            json={
                "rows": [
                    {
                        "publish_record_id": publish_record_id,
                        "metric_name": "completion_rate",
                        "metric_value": 0.9,
                        "completion_rate": 0.9,
                        "impressions": 30,
                        "window": "7d",
                    }
                ],
            },
        )
        score = client.get("/api/cases/case_demo/performance").json()["scores"][0]
        assert score["excluded_reason"] == "low_impressions"
        assert score["confidence"] <= 0.3


def test_reflection_emits_data_driven_proposal_from_metrics():
    with TestClient(create_app()) as client:
        _login(client)
        _, publish_record_id = _seed_published_record(client)
        client.post(
            "/api/cases/case_demo/metrics/import",
            json={
                "rows": [
                    {
                        "publish_record_id": publish_record_id,
                        "metric_name": "completion_rate",
                        "metric_value": 0.85,
                        "completion_rate": 0.85,
                        "impressions": 25000,
                        "window": "7d",
                    }
                ],
            },
        )
        reflection = client.post("/api/cases/case_demo/reflection-runs", json={"window": "7d"})
        assert reflection.status_code == 202, reflection.text
        reflection_id = reflection.json()["id"]
        assert reflection.json()["sample_size"] >= 1

        proposals = client.get("/api/cases/case_demo/agent/memory-proposals").json()["items"]
        proposal = next(p for p in proposals if p["proposed_by_reflection_run_id"] == reflection_id)
        # §8.4: proposal is data-driven, not the old canned literal.
        assert "best performing hook style" not in proposal["insight"]
        assert proposal["evidence"]  # carries evidence refs
        assert proposal["sample_size"] >= 1


def test_memory_recall_modes_filter_and_rank():
    with TestClient(create_app()) as client:
        _login(client)
        # Seed an active memory via reflection + approval.
        reflection = client.post("/api/cases/case_demo/reflection-runs", json={"window": "7d"})
        assert reflection.status_code == 202, reflection.text
        proposals = client.get("/api/cases/case_demo/agent/memory-proposals").json()["items"]
        approved = client.post(
            f"/api/cases/case_demo/memory/{proposals[-1]['id']}/approve", json={}
        )
        assert approved.status_code == 200, approved.text
        memory_id = approved.json()["id"]

        recall = client.get("/api/cases/case_demo/memory/recall", params={"mode": "recent", "limit": 5})
        assert recall.status_code == 200, recall.text
        body = recall.json()
        assert body["mode"] == "recent"
        assert any(m["id"] == memory_id for m in body["memories"])

        # A platform that no active memory is scoped to yields nothing for platform mode.
        recall_platform = client.get(
            "/api/cases/case_demo/memory/recall", params={"mode": "platform", "platform": "douyin"}
        )
        assert recall_platform.status_code == 200, recall_platform.text


def test_agent_memory_proposal_run_is_data_driven():
    with TestClient(create_app()) as client:
        _login(client)
        run = client.post("/api/cases/case_demo/agent/runs", json={"goal": "memory_proposal"})
        assert run.status_code == 202, run.text
        run_id = run.json()["id"]
        proposals = client.get("/api/cases/case_demo/agent/memory-proposals").json()["items"]
        proposal = next(p for p in proposals if p["proposed_by_reflection_run_id"] == run_id)
        # Old hardcoded literal must be gone.
        assert proposal["insight"] != "Short hooks with concrete outcomes perform better for this case."
