from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.main import app


client = TestClient(app)


def login_admin_for(active_client):
    response = active_client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def video_payload(title: str):
    return {
        "case_id": "case_demo",
        "title": title,
        "script": "用一个简短脚本补齐发布测试。",
        "voice": {"voice_id": "voice_sandbox"},
        "portrait": {"template_mode": "agent"},
        "strictness": {"strict_timestamps": False},
    }


def create_finished_video(active_client, title: str = "Publishing seed") -> str:
    created = active_client.post("/api/jobs/digital-human-video", json=video_payload(title))
    assert created.status_code == 201, created.text
    assert created.json()["initial_run"]["status"] == "succeeded"
    videos = active_client.get("/api/cases/case_demo/finished-videos").json()["items"]
    return videos[-1]["id"]


def create_publish_batch(active_client, finished_video_id: str):
    package = active_client.post(
        "/api/publish/packages",
        json={"source_finished_video_id": finished_video_id, "title": "Publish me", "description": ""},
    )
    assert package.status_code == 201, package.text
    batch = active_client.post(
        "/api/publish/batches",
        json={"publish_package_ids": [package.json()["id"]], "platform_targets": ["xiaovmao"]},
    )
    assert batch.status_code == 201, batch.text
    return batch.json()


def test_case_reflection_memory_approval_and_publish_flow():
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text
    reflection = client.post(
        "/api/cases/case_demo/reflection-runs",
        json={"window": "7d", "force": True},
    )
    assert reflection.status_code == 202, reflection.text
    proposals = client.get("/api/cases/case_demo/agent/memory-proposals").json()["items"]
    assert proposals
    memory = client.post(
        f"/api/cases/case_demo/memory/{proposals[-1]['id']}/approve",
        json={"reason": "golden approval"},
    ).json()
    assert memory["status"] == "active"

    videos = client.get("/api/cases/case_demo/finished-videos").json()["items"]
    if not videos:
        client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "Publishing seed",
                "script": "用一个简短脚本补齐发布测试。",
                "voice": {"voice_id": "voice_sandbox"},
                "portrait": {"template_mode": "agent"},
                "strictness": {"strict_timestamps": False},
            },
        )
        videos = client.get("/api/cases/case_demo/finished-videos").json()["items"]
    package = client.post(
        "/api/publish/packages",
        json={"source_finished_video_id": videos[-1]["id"], "title": "Publish me", "description": ""},
    ).json()
    batch = client.post(
        "/api/publish/batches",
        json={"publish_package_ids": [package["id"]], "platform_targets": ["xiaovmao"]},
    ).json()
    submitted = client.post(f"/api/publish/batches/{batch['id']}/submit", json={"dry_run": False}).json()
    assert submitted["status"] == "completed"
    assert submitted["items"][0]["status"] == "published"

    ops = client.get("/api/ops/dashboard").json()
    assert "usage" in ops
    assert "yield_funnel" in ops


def test_spec_20_2_12_publish_failure_can_retry_publish_successfully():
    """Spec 20.2 #12: sandbox publish failure can be retried through retry-publish."""
    with TestClient(create_app()) as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "Retry publish seed")
        batch = create_publish_batch(active_client, finished_video_id)
        failed = active_client.post(
            f"/api/publish/batches/{batch['id']}/submit",
            json={"dry_run": False, "simulate_publish_failure": True},
        )
        assert failed.status_code == 202, failed.text
        failed_body = failed.json()
        assert failed_body["status"] == "partial_failed"
        item = failed_body["items"][0]
        assert item["status"] == "publish_failed"

        retried = active_client.post(
            f"/api/publish/batches/{batch['id']}/items/{item['id']}/retry-publish",
            json={},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "published"


def test_spec_20_2_16_case_reflection_after_five_published_videos_creates_memory_proposal():
    """Spec 20.2 #16: five published videos can trigger reflection memory proposal generation."""
    with TestClient(create_app()) as active_client:
        login_admin_for(active_client)
        for index in range(5):
            finished_video_id = create_finished_video(active_client, f"Reflection seed {index}")
            batch = create_publish_batch(active_client, finished_video_id)
            submitted = active_client.post(f"/api/publish/batches/{batch['id']}/submit", json={"dry_run": False})
            assert submitted.status_code == 202, submitted.text
            assert submitted.json()["items"][0]["status"] == "published"

        reflection = active_client.post(
            "/api/cases/case_demo/reflection-runs",
            json={"window": "7d", "force": True},
        )
        assert reflection.status_code == 202, reflection.text
        reflection_id = reflection.json()["id"]
        proposals = active_client.get("/api/cases/case_demo/agent/memory-proposals").json()["items"]
        proposal = next(item for item in proposals if item.get("proposed_by_reflection_run_id") == reflection_id)
        assert proposal["status"] == "proposed"
        assert proposal["evidence"]
