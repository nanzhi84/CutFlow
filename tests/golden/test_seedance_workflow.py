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

from contextlib import contextmanager

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.contracts import ArtifactKind
from packages.core.storage.database import ArtifactRow, MediaAssetRow


@contextmanager
def fresh_client():
    """A per-test app/client whose SQLAlchemy engine is disposed on exit.

    Each test gets its own in-memory workflow ``runtime_repository`` (aligned with
    the per-test SQL reset) and releases its connection pool before teardown
    TRUNCATEs the database, so a long golden run never deadlocks against it.
    """
    app = create_app()
    # Do NOT enter the TestClient lifespan: app.state is fully configured at build
    # time. Entering would re-run bootstrap_sqlalchemy_storage (a full seed_database
    # merge over users/registration_codes/provider_profiles/media_assets) on every
    # test, contending on exactly the tables the conftest teardown TRUNCATEs and
    # deadlocking under a long golden run. Skipping it also avoids extra engine churn.
    active_client = TestClient(app)
    try:
        yield active_client
    finally:
        engine = app.state.sqlalchemy_session_factory.kw.get("bind")
        if engine is not None:
            engine.dispose()


def login_admin_for(active_client):
    response = active_client.post(
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


def _finished_videos_for_run(active_client, run_id: str):
    repository = active_client.app.state.repository
    return [video for video in repository.finished_videos.values() if video.run_id == run_id]


def test_seedance_pure_text_to_video_creates_finished_video():
    with fresh_client() as active_client:
        login_admin_for(active_client)
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=_seedance_payload(),
            headers={"Idempotency-Key": "seedance-t2v-smoke"},
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "succeeded", run
        report = active_client.get(f"/api/runs/{run['id']}/report").json()
        assert report["public_report"]["status"] == "succeeded"
        assert _finished_videos_for_run(active_client, run["id"])


def test_seedance_with_reference_image_creates_finished_video():
    with fresh_client() as active_client:
        login_admin_for(active_client)
        # Seed a reference image asset backed by a source artifact so the node's
        # reference resolution (source_artifact_for_asset) succeeds.
        repo = active_client.app.state.repository
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
        session_factory = active_client.app.state.sqlalchemy_session_factory
        with session_factory() as session:
            session.add(
                ArtifactRow(
                    id=artifact.id,
                    case_id=artifact.case_id,
                    kind=artifact.kind.value,
                    uri=artifact.uri,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                    media_info=artifact.media_info.model_dump(mode="json")
                    if artifact.media_info is not None
                    else None,
                    payload_schema=artifact.payload_schema,
                    payload=artifact.payload,
                )
            )
            session.add(
                MediaAssetRow(
                    id=asset.id,
                    case_id=asset.case_id,
                    title=asset.title,
                    kind=asset.kind,
                    source_artifact_id=asset.source_artifact_id,
                    tags=asset.tags,
                    annotation_status=asset.annotation_status,
                    usable=asset.usable,
                    duration_sec=asset.duration_sec,
                    width=asset.width,
                    height=asset.height,
                )
            )
            session.commit()

        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=_seedance_payload(reference_asset_ids=[asset.id]),
            headers={"Idempotency-Key": "seedance-i2v-smoke"},
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "succeeded", run
        videos = _finished_videos_for_run(active_client, run["id"])
        assert videos
        assert videos[0].video_artifact is not None
