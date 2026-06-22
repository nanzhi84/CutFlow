"""End-to-end (sandbox) smoke for the seedance_t2v_v1 chain.

Drives the real FastAPI app + in-memory pipeline: posting a job with
``workflow_template_id="seedance_t2v_v1"`` must run ValidateRequest ->
LoadCaseContext -> SeedanceGenerateVideo -> ExportSeedanceVideo ->
FinalizeRunReport to ``succeeded`` and produce a FinishedVideo + publish package,
using the seeded ``sandbox.video.default`` provider (no real Ark key needed —
conftest enables sandbox fallback). Covers both pure text-to-video and the
mandatory reference-image path (reference_asset_ids resolved to a source artifact).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.main import app, repository
from packages.core import contracts as c
from packages.core.contracts import ArtifactKind

client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _seedance_payload(**overrides):
    payload = {
        "case_id": "case_demo",
        "title": "Seedance 冒烟",
        "script": "门头特写，暖光，产品整齐陈列，镜头缓推。",
        "voice": {"voice_id": ""},
        "workflow_template_id": "seedance_t2v_v1",
        "reference_asset_ids": [],
    }
    payload.update(overrides)
    return payload


def _finished_videos_for_run(run_id: str):
    return [video for video in repository().finished_videos.values() if video.run_id == run_id]


def test_seedance_pure_text_to_video_creates_finished_video():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=_seedance_payload(),
        headers={"Idempotency-Key": "seedance-t2v-smoke"},
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]
    assert run["status"] == "succeeded", run
    report = client.get(f"/api/runs/{run['id']}/report").json()
    assert report["public_report"]["status"] == "succeeded"
    assert _finished_videos_for_run(run["id"])


def test_seedance_with_reference_image_creates_finished_video():
    login_admin()
    # Seed a reference image asset backed by a source artifact so the node's
    # reference resolution (source_artifact_for_asset) succeeds.
    repo = repository()
    artifact = repo.create_artifact(
        kind=ArtifactKind.uploaded_file,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        uri="local://cutagent-local/seedance-reference.png",
    )
    asset = c.MediaAssetRecord(
        id="asset_seedance_ref",
        case_id="case_demo",
        title="门头参考图",
        kind="image",
        source_artifact_id=artifact.id,
    )
    repo.media_assets[asset.id] = asset

    response = client.post(
        "/api/jobs/digital-human-video",
        json=_seedance_payload(reference_asset_ids=[asset.id]),
        headers={"Idempotency-Key": "seedance-i2v-smoke"},
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]
    assert run["status"] == "succeeded", run
    videos = _finished_videos_for_run(run["id"])
    assert videos
    assert videos[0].video_artifact is not None
