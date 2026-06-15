"""Publishing copy / cover / preview-frame / platform-accounts API endpoints (§28.3).

Exercises the in-memory backend with a real local publish video so the cover node
runs ffmpeg frame extraction end-to-end.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.fixtures.media import generate_test_video

client = TestClient(app)


def _login_admin() -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _upload_publish_video(tmp_path) -> str:
    video_path = generate_test_video(tmp_path, duration_sec=2, width=320, height=568, fps=24)
    content = video_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "publish_video",
            "case_id": "case_demo",
            "filename": "publish.mp4",
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("publish.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["artifact"]["artifact_id"]


def _make_batch(tmp_path) -> tuple[str, str]:
    artifact_id = _upload_publish_video(tmp_path)
    package = client.post(
        "/api/publish/packages",
        json={"upload_artifact_id": artifact_id, "title": "今天分享一个汽车补漆案例，效果惊艳省钱省心", "description": ""},
    )
    assert package.status_code == 201, package.text
    batch = client.post(
        "/api/publish/batches",
        json={"publish_package_ids": [package.json()["id"]], "platform_targets": ["douyin"]},
    )
    assert batch.status_code == 201, batch.text
    body = batch.json()
    return body["id"], body["items"][0]["id"]


def test_generate_copy_populates_item_copy_fields(tmp_path):
    _login_admin()
    batch_id, item_id = _make_batch(tmp_path)

    response = client.post(
        f"/api/publish/batches/{batch_id}/items/{item_id}/generate-copy",
        json={"overwrite": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "deterministic"
    assert body["title"]
    assert body["cover_title"]

    detail = client.get(f"/api/publish/batches/{batch_id}").json()
    item = next(i for i in detail["items"] if i["id"] == item_id)
    assert item["cover_title"]
    assert item["publish_content"]


def test_generate_cover_uses_frame_fallback_without_ai(tmp_path):
    _login_admin()
    batch_id, item_id = _make_batch(tmp_path)

    response = client.post(
        f"/api/publish/batches/{batch_id}/items/{item_id}/generate-cover",
        json={"mode": "ai", "frame_time_sec": 0.5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # No image provider armed -> honest frame fallback flagged per §2.2.
    assert body["source"] == "frame"
    assert body["frame_fallback"] is True
    assert body["degraded_reason"] == "cover.frame_fallback"
    assert body["cover_artifact"]["artifact_id"]

    detail = client.get(f"/api/publish/batches/{batch_id}").json()
    item = next(i for i in detail["items"] if i["id"] == item_id)
    assert item["cover_artifact_id"] == body["cover_artifact"]["artifact_id"]


def test_preview_cover_frame_extracts_frame(tmp_path):
    _login_admin()
    batch_id, item_id = _make_batch(tmp_path)

    response = client.post(
        f"/api/publish/batches/{batch_id}/items/{item_id}/preview-cover-frame",
        json={"frame_time_sec": 1.0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["frame_time_sec"] == 1.0
    assert body["frame_artifact"]["artifact_id"]


def test_platform_accounts_lists_sandbox_stub_accounts(tmp_path):
    _login_admin()
    response = client.get("/api/publish/platform-accounts", params={"case_name": "树影"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["adapter_id"] == "sandbox.publish"
    assert body["available"] is True
    assert {a["platform"] for a in body["accounts"]} == {"douyin", "kuaishou", "shipinhao", "xiaohongshu"}


def test_patch_item_persists_tags_and_location(tmp_path):
    _login_admin()
    batch_id, item_id = _make_batch(tmp_path)

    response = client.patch(
        f"/api/publish/items/{item_id}",
        json={"tags": ["#补漆, 汽车", "汽车"], "location": "上海", "account_group": "树影"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tags"] == ["补漆", "汽车"]  # normalized + deduped
    assert body["location"] == "上海"
    assert body["account_group"] == "树影"


def test_scheduled_submit_records_scheduled_attempt(tmp_path):
    _login_admin()
    batch_id, item_id = _make_batch(tmp_path)

    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    response = client.post(
        f"/api/publish/batches/{batch_id}/submit",
        json={"mode": "scheduled", "scheduled_at": future},
    )
    assert response.status_code == 202, response.text

    attempts = client.get(f"/api/publish/batches/{batch_id}/attempts").json()["items"]
    assert attempts
    assert attempts[0]["status"] == "scheduled"


def test_scheduled_submit_rejects_past_time(tmp_path):
    _login_admin()
    batch_id, item_id = _make_batch(tmp_path)

    response = client.post(
        f"/api/publish/batches/{batch_id}/submit",
        json={"mode": "scheduled", "scheduled_at": "2000-01-01T00:00:00+08:00"},
    )
    assert response.status_code == 400, response.text
