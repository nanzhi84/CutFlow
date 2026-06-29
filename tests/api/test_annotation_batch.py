"""Batch annotation (批量标注) endpoint end-to-end on the in-memory path (Spec §2.1)."""

import hashlib

from fastapi.testclient import TestClient

from apps.api.main import app, repository
from packages.core.storage.database import CaseRow, MediaAssetRow
from tests.fixtures.media import generate_test_video

client = TestClient(app)


def _login_admin() -> None:
    response = client.post("/api/auth/login", json={"email": "admin@local.cutagent", "password": "local-admin"})
    assert response.status_code == 200, response.text


def _seed_case(case_id: str) -> None:
    """Ensure the case row exists in Postgres (FK on upload_sessions.case_id -> cases)."""
    with app.state.sqlalchemy_session_factory() as session:
        if session.get(CaseRow, case_id) is not None:
            return
        session.add(CaseRow(id=case_id, name=case_id, owner_user_id="usr_admin", status="active"))
        session.commit()


def _upload_asset(tmp_path, *, filename: str, case_id: str) -> str:
    _seed_case(case_id)
    video = generate_test_video(tmp_path, duration_sec=1, width=160, height=120, fps=15, filename=filename)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "broll",
            "case_id": case_id,
            "filename": filename,
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    client.put(f"/api/uploads/{upload['id']}/file", files={"file": (filename, content, "video/mp4")})
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest, "metadata": {"title": filename}},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["media_asset"]["id"]


def test_batch_annotation_runs_gated_pipeline_over_assets(tmp_path):
    _login_admin()
    a1 = _upload_asset(tmp_path, filename="batch-a.mp4", case_id="case_batch")
    a2 = _upload_asset(tmp_path, filename="batch-b.mp4", case_id="case_batch")

    response = client.post(
        "/api/annotations/batch",
        json={"schema_version": "annotation_batch_request.v1", "asset_ids": [a1, a2], "force": True},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_id"]
    statuses = {item["asset_id"]: item["status"] for item in body["results"]}
    # No real vlm.annotation provider -> degraded but completed (never fabricated).
    assert statuses[a1] == "completed"
    assert statuses[a2] == "completed"
    assert body["completed_count"] == 2
    # The annotation_batch Job was created and persisted.
    assert body["job_id"] in repository().jobs
    assert repository().jobs[body["job_id"]].type.value == "annotation_batch"
    # A real AnnotationV4 canonical now exists for each asset (the must-retain artifact).
    for asset_id in (a1, a2):
        editor = client.get(f"/api/annotations/{asset_id}").json()
        assert editor["canonical"]["meta"]["asset_id"] == asset_id


def test_batch_annotation_skips_already_annotated_when_not_forced(tmp_path):
    _login_admin()
    asset_id = _upload_asset(tmp_path, filename="batch-skip.mp4", case_id="case_batch_skip")
    # Mark the asset as already annotated (a prior real annotation pass). The skip
    # gate reads annotation_status from the SQL media row, so update it there.
    with app.state.sqlalchemy_session_factory() as session:
        row = session.get(MediaAssetRow, asset_id)
        row.annotation_status = "annotated"
        session.commit()

    # force=False skips the already-annotated asset.
    skipped = client.post(
        "/api/annotations/batch",
        json={"schema_version": "annotation_batch_request.v1", "asset_ids": [asset_id], "force": False},
    )
    assert skipped.status_code == 202, skipped.text
    body = skipped.json()
    assert body["results"][0]["status"] == "skipped"
    assert body["skipped_count"] == 1

    # force=True re-annotates it (no skip).
    forced = client.post(
        "/api/annotations/batch",
        json={"schema_version": "annotation_batch_request.v1", "asset_ids": [asset_id], "force": True},
    )
    assert forced.json()["results"][0]["status"] != "skipped"


def test_batch_annotation_filters_by_material_type(tmp_path):
    _login_admin()
    asset_id = _upload_asset(tmp_path, filename="batch-mt.mp4", case_id="case_batch_mt")  # kind=broll
    response = client.post(
        "/api/annotations/batch",
        json={
            "schema_version": "annotation_batch_request.v1",
            "asset_ids": [asset_id],
            "force": True,
            "material_type": "portrait",
        },
    )
    assert response.status_code == 202, response.text
    item = response.json()["results"][0]
    assert item["status"] == "skipped"


def test_batch_annotation_reports_missing_asset(tmp_path):
    _login_admin()
    response = client.post(
        "/api/annotations/batch",
        json={"schema_version": "annotation_batch_request.v1", "asset_ids": ["asset_does_not_exist"], "force": True},
    )
    assert response.status_code == 202, response.text
    item = response.json()["results"][0]
    assert item["status"] == "failed"
    assert item["error_code"] == "artifact.missing"
