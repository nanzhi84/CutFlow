from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.main import repository
from packages.core.contracts import ArtifactKind


client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def video_payload(**overrides):
    payload = {
        "case_id": "case_demo",
        "title": "Golden success",
        "script": "先指出低效内容生产的痛点。再展示 Case Memory 如何复用历史经验。最后邀请运营查看报告。",
        "voice": {"voice_id": "voice_sandbox"},
        "portrait": {"template_mode": "agent"},
        "broll": {"enabled": False, "max_inserts": 2},
        "bgm": {"enabled": False},
        "subtitle": {"enabled": True},
        "lipsync": {"enabled": True, "provider_profile_id": "runninghub.heygem.default"},
        "strictness": {"strict_timestamps": False},
    }
    payload.update(overrides)
    return payload


def test_minimal_success_video_creates_finished_video_and_report():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(),
        headers={"Idempotency-Key": "golden-video-success"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    replayed = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(),
        headers={"Idempotency-Key": "golden-video-success"},
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["job"]["id"] == body["job"]["id"]
    assert replayed.json()["initial_run"]["id"] == body["initial_run"]["id"]
    run = body["initial_run"]
    assert run["status"] == "succeeded"
    report = client.get(f"/api/runs/{run['id']}/report").json()
    assert report["public_report"]["status"] == "succeeded"
    videos = client.get("/api/cases/case_demo/finished-videos").json()["items"]
    assert videos


def test_broll_missing_is_soft_degrade_and_reported():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(
            title="B-roll degraded",
            broll={"enabled": True, "max_inserts": 2, "case_id": "case_without_broll"},
        ),
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]
    assert run["status"] == "succeeded"
    report = client.get(f"/api/runs/{run['id']}/report").json()["public_report"]
    assert "broll.skipped_no_material" in report["degradations"]


def test_portrait_missing_is_hard_fail():
    login_admin()
    case = client.post("/api/cases", json={"name": "No portrait case"}).json()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(case_id=case["id"], title="Hard fail"),
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]
    assert run["status"] == "failed"
    detail = client.get(f"/api/runs/{run['id']}").json()
    errors = [node.get("error") for node in detail["node_runs"] if node.get("error")]
    assert errors[-1]["code"] == "material.insufficient.portrait"


def test_pipeline_writes_typed_artifact_payloads_with_frame_quantized_timeline():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(title="Typed artifacts"),
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]

    artifacts = {
        artifact.kind: artifact
        for artifact in repository().artifacts.values()
        if artifact.run_id == run["id"]
    }
    narration = artifacts[ArtifactKind.narration_units].payload
    assert narration["source"] == "estimated"
    assert narration["strict"] is False
    assert all({"unit_id", "start", "end", "confidence"} <= set(unit) for unit in narration["units"])

    timeline = artifacts[ArtifactKind.timeline_plan].payload
    assert timeline["fps"] == 30
    assert timeline["total_frames"] > 0
    assert isinstance(timeline["tracks"], list)
    assert all(isinstance(segment["timeline_start_frame"], int) for segment in timeline["tracks"])
    assert timeline["validation"]["checks"] == {
        "overlap": True,
        "negative_duration": True,
        "out_of_bounds": True,
    }


def test_strict_alignment_rejects_estimated_narration_units():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(title="Strict timestamps", strictness={"strict_timestamps": True}),
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]
    assert run["status"] == "failed"
    detail = client.get(f"/api/runs/{run['id']}").json()
    errors = [node.get("error") for node in detail["node_runs"] if node.get("error")]
    assert errors[-1]["code"] == "render.invalid_timeline"


def test_resume_from_failed_job_is_rejected_by_state_machine():
    login_admin()
    case = client.post("/api/cases", json={"name": "Resume case"}).json()
    failed = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(case_id=case["id"], title="Resume hard fail"),
    ).json()
    failed_run = failed["initial_run"]
    resumed = client.post(
        f"/api/runs/{failed_run['id']}/resume",
        json={"reason": "verify resume prefix", "reuse_valid_artifacts": True},
    )
    assert resumed.status_code == 400
    assert resumed.json()["error"]["code"] == "workflow.invalid_transition"
