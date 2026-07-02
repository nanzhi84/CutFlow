import hashlib
import io
import math
import struct
import wave

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    FinishedVideo,
    Job,
    JobType,
    MediaInfo,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.database import ArtifactRow, CaseRow, MediaAssetRow
from packages.core.storage.repository import Repository, new_id
from packages.core.storage.sqlalchemy_uploads import artifact_row_to_contract
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import probe_media
from tests.fixtures.media import generate_test_video, require_ffmpeg_filters
from tests.api._upload_helpers import direct_upload
from packages.core.storage.object_store import parse_local_uri


client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _seed_case(case_id: str) -> None:
    """Insert a shared case row so upload sessions can satisfy the
    ``upload_sessions.case_id -> cases.id`` FK that the SQL backend enforces (the
    removed in-memory backend did not). Idempotent."""
    with app.state.sqlalchemy_session_factory() as session:
        if session.get(CaseRow, case_id) is not None:
            return
        session.add(CaseRow(id=case_id, name="上传测试案例", status="active"))
        session.commit()


def _get_artifact(artifact_id: str) -> Artifact:
    """Read a persisted artifact back from Postgres. The API writes through to SQL,
    so assertions must query the database rather than the runtime repository."""
    with app.state.sqlalchemy_session_factory() as session:
        row = session.get(ArtifactRow, artifact_id)
        assert row is not None, f"artifact {artifact_id} not persisted"
        return artifact_row_to_contract(row)


def _artifact_exists(artifact_id: str) -> bool:
    with app.state.sqlalchemy_session_factory() as session:
        return session.get(ArtifactRow, artifact_id) is not None


def _cover_artifacts_for_source(source_artifact_id: str) -> list[Artifact]:
    """All persisted cover_image artifacts derived from the given source artifact."""
    with app.state.sqlalchemy_session_factory() as session:
        rows = (
            session.execute(
                select(ArtifactRow).where(ArtifactRow.kind == ArtifactKind.cover_image.value)
            )
            .scalars()
            .all()
        )
        artifacts = [artifact_row_to_contract(row) for row in rows]
    return [
        artifact
        for artifact in artifacts
        if (artifact.payload or {}).get("source_artifact_id") == source_artifact_id
    ]


def _media_asset_source_and_tags(asset_id: str) -> tuple[str | None, list[str]]:
    with app.state.sqlalchemy_session_factory() as session:
        row = session.get(MediaAssetRow, asset_id)
        assert row is not None, f"media asset {asset_id} not persisted"
        return row.source_artifact_id, list(row.tags)


def _wav_bytes(duration_sec: float = 0.25, sample_rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for i in range(int(sample_rate * duration_sec)):
            t = i / sample_rate
            frames.extend(struct.pack("<h", int(0.2 * 32767 * math.sin(2 * math.pi * 220 * t))))
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


def test_upload_flow_uses_object_store_uri_and_validates_integrity():
    login_admin()
    _seed_case("case_stabilize_upload")
    # The original used a portrait + text/plain body purely to exercise object-store
    # URIs + integrity + media-asset creation without ffmpeg. text/plain is now
    # rejected by the per-kind content-type allowlist, so use bgm + a real
    # (ffmpeg-free) WAV: it passes the allowlist, ffprobe parses it, and complete
    # still auto-creates a MediaAsset, preserving the original intent.
    content = _wav_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared, completed = direct_upload(
        client,
        kind="bgm",
        case_id="case_stabilize_upload",
        filename="sample.wav",
        content_type="audio/wav",
        body=content,
        sha256=digest,
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    assert upload["upload_session"]["object_uri"].startswith("local://")
    assert upload["put_url"].startswith("local://")

    assert completed.status_code == 200, completed.text
    artifact = completed.json()["artifact"]
    assert artifact["kind"] == "uploaded.file"
    assert completed.json()["media_asset"]["kind"] == "bgm"
    assert completed.json()["media_asset"]["source_artifact_id"] == artifact["artifact_id"]


def test_create_media_asset_from_completed_upload_points_to_uploaded_artifact():
    login_admin()
    content = _wav_bytes()
    prepared, completed = direct_upload(
        client,
        kind="bgm",
        case_id="case_demo",
        filename="asset-create.wav",
        content_type="audio/wav",
        body=content,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    upload_session_id = prepared.json()["upload_session"]["id"]
    artifact_id = completed.json()["artifact"]["artifact_id"]

    created = client.post(
        "/api/media/assets",
        json={
            "upload_session_id": upload_session_id,
            "case_id": "case_demo",
            "title": "Created from upload",
            "kind": "bgm",
            "tags": ["bgm"],
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["source_artifact_id"] == artifact_id
    assert _artifact_exists(created.json()["source_artifact_id"])


def test_local_media_preview_url_is_browser_playable_content_route():
    login_admin()
    content = _wav_bytes()
    prepared, completed = direct_upload(
        client,
        kind="bgm",
        filename="preview.wav",
        content_type="audio/wav",
        body=content,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    asset_id = completed.json()["media_asset"]["id"]

    preview = client.get(f"/api/media/assets/{asset_id}/preview-url")
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["url"] == f"/api/media/assets/{asset_id}/content"
    assert preview_body["playable"] is True
    assert preview_body["content_type"].startswith("audio/")

    media = client.get(preview_body["url"])
    assert media.status_code == 200, media.text
    assert media.headers["content-disposition"].startswith("inline;")
    assert media.content == content


def test_finished_video_preview_url_is_browser_playable_stream_route():
    login_admin()
    content = b"cutagent test mp4 payload"
    store = app.state.object_store
    stored = store.put_bytes(
        store.prepare_upload("finished-preview.mp4", "finished-video"),
        content,
    )
    repo = Repository()
    case_id = "case_demo"
    job = Job(
        id=new_id("job"),
        type=JobType.digital_human_video,
        case_id=case_id,
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id=case_id,
            script="preview test",
            voice={"voice_id": "voice_sandbox"},
        ),
    )
    run = WorkflowRun(
        id=new_id("run"),
        job_id=job.id,
        case_id=case_id,
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.succeeded,
    )
    repo.jobs[job.id] = job.model_copy(update={"active_run_id": run.id})
    repo.runs[run.id] = run
    repo.node_runs[run.id] = []
    artifact = repo.create_artifact(
        kind=ArtifactKind.video_finished,
        payload_schema="uri-only",
        payload=None,
        case_id=case_id,
        run_id=run.id,
        uri=stored.ref.uri,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        media_info=MediaInfo(media_type="video", codec="h264", format="mp4", mime_type="video/mp4"),
    )
    finished = FinishedVideo(
        id=new_id("fv"),
        case_id=case_id,
        run_id=run.id,
        title="Preview route video",
        video_artifact=repo.artifact_ref(artifact.id),
    )
    repo.finished_videos[finished.id] = finished
    app.state.sqlalchemy_production_repository.sync_workflow_snapshot(
        job=repo.jobs[job.id], run=run, repository=repo
    )

    preview = client.get(f"/api/finished-videos/{finished.id}/preview-url")
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["url"] == f"/api/finished-videos/{finished.id}/stream"
    assert preview_body["playable"] is True
    assert preview_body["content_type"] == "video/mp4"

    media = client.get(preview_body["url"])
    assert media.status_code == 200, media.text
    assert media.headers["content-disposition"].startswith("inline;")
    assert media.content == content


def _register_finished_video(uri: str, *, title: str) -> str:
    # A dedicated case so these (deliberately non-downloadable) finished videos
    # never leak into case_demo, whose last finished video other suites publish.
    case_id = "case_stream_proxy_test"
    _seed_case(case_id)
    repo = Repository()
    job = Job(
        id=new_id("job"),
        type=JobType.digital_human_video,
        case_id=case_id,
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id=case_id,
            script="stream proxy",
            voice={"voice_id": "voice_sandbox"},
        ),
    )
    run = WorkflowRun(
        id=new_id("run"),
        job_id=job.id,
        case_id=case_id,
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.succeeded,
    )
    repo.jobs[job.id] = job.model_copy(update={"active_run_id": run.id})
    repo.runs[run.id] = run
    repo.node_runs[run.id] = []
    artifact = repo.create_artifact(
        kind=ArtifactKind.video_finished,
        payload_schema="uri-only",
        payload=None,
        case_id=case_id,
        run_id=run.id,
        uri=uri,
        media_info=MediaInfo(media_type="video", codec="h264", format="mp4", mime_type="video/mp4"),
    )
    finished = FinishedVideo(
        id=new_id("fv"),
        case_id=case_id,
        run_id=run.id,
        title=title,
        video_artifact=repo.artifact_ref(artifact.id),
    )
    repo.finished_videos[finished.id] = finished
    app.state.sqlalchemy_production_repository.sync_workflow_snapshot(
        job=repo.jobs[job.id], run=run, repository=repo
    )
    return finished.id


def test_finished_video_preview_keeps_signed_url_for_s3_durable():
    # An s3:// durable object (e.g. Aliyun OSS) must NOT be routed through the
    # same-origin /stream proxy: the browser streams it directly from a presigned
    # URL (native range support, no blocking download-through the API server).
    login_admin()
    s3_uri = "s3://cutagent-prod/finished/oss-finished-video.mp4"
    finished_id = _register_finished_video(s3_uri, title="OSS finished video")

    preview = client.get(f"/api/finished-videos/{finished_id}/preview-url")
    assert preview.status_code == 200, preview.text
    preview_url = preview.json()["url"]
    assert preview_url != f"/api/finished-videos/{finished_id}/stream"
    assert preview_url.startswith("s3://")


def test_finished_video_stream_missing_bytes_returns_error():
    # A registered local:// finished video whose bytes are absent must fail
    # loudly (not 200 an empty/garbage body).
    login_admin()
    finished_id = _register_finished_video(
        "local://cutagent-local/finished/never-materialized.mp4",
        title="Missing bytes video",
    )

    stream = client.get(f"/api/finished-videos/{finished_id}/stream")
    assert stream.status_code >= 400


def test_upload_complete_rejects_size_mismatch_against_head():
    # The proxy byte-ingress (PUT /file) and its per-PUT size check are gone.
    # The new guard is in complete(): it HEAD-verifies the object the browser PUT
    # directly to OSS against the declared size, and must fail loudly when they
    # disagree (no artifact registered).
    login_admin()
    body = b"short"  # 5 bytes
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "font",
            "filename": "short.ttf",
            "content_type": "font/ttf",
            "size_bytes": 100,
        },
    )
    assert prepared.status_code == 201, prepared.text
    ticket = prepared.json()
    # The browser's direct PUT writes only 5 bytes, not the declared 100.
    app.state.object_store.put_bytes(parse_local_uri(ticket["put_url"]), body)
    response = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": ticket["upload_session"]["id"]},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "upload.size_mismatch"


def test_publish_video_upload_creates_publish_package(tmp_path):
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568)
    content = video.read_bytes()
    prepared, completed = direct_upload(
        client,
        kind="publish_video",
        case_id="case_demo",
        filename="publish.mp4",
        content_type="video/mp4",
        body=content,
        metadata={"title": "Publish upload"},
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    assert completed.json()["media_asset"] is None
    assert completed.json()["publish_package"]["upload_artifact_id"] == completed.json()["artifact"]["artifact_id"]
    assert completed.json()["publish_package"]["platform_defaults"]["title"] == "Publish upload"


def test_video_upload_probes_media_and_creates_real_thumbnail_artifacts(tmp_path):
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared, completed = direct_upload(
        client,
        kind="video",
        case_id="case_demo",
        filename="portrait.mp4",
        content_type="video/mp4",
        body=content,
        sha256=digest,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    artifact_id = completed.json()["artifact"]["artifact_id"]
    uploaded_artifact = _get_artifact(artifact_id)
    assert uploaded_artifact.sha256 == digest
    assert uploaded_artifact.media_info is not None
    assert uploaded_artifact.media_info.media_type == "video"
    assert uploaded_artifact.media_info.width == 320
    thumbnails = _cover_artifacts_for_source(artifact_id)
    assert {artifact.payload["thumbnail_label"] for artifact in thumbnails} == {"first", "mid"}
    assert all(artifact.sha256 for artifact in thumbnails)
    assert all(artifact.media_info and artifact.media_info.media_type == "image" for artifact in thumbnails)


def test_unified_video_kind_upload_creates_video_media_asset(tmp_path):
    """P0: the unified ``video`` bucket creates a media asset (kind=video) through the
    same probe/normalize path as portrait/broll — the operator never picks A/B-roll."""
    login_admin()
    _seed_case("case_unified_video")
    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568)
    content = video.read_bytes()
    prepared, completed = direct_upload(
        client,
        kind="video",
        case_id="case_unified_video",
        filename="mixed.mp4",
        content_type="video/mp4",
        body=content,
    )
    assert prepared.status_code == 201, prepared.text
    assert prepared.json()["upload_session"]["kind"] == "video"
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["media_asset"] is not None
    assert body["media_asset"]["kind"] == "video"
    assert body["media_asset"]["source_artifact_id"] == body["artifact"]["artifact_id"]
    artifact = _get_artifact(body["artifact"]["artifact_id"])
    assert artifact.media_info is not None and artifact.media_info.media_type == "video"


def test_visual_asset_kind_and_tags_passthrough_for_valid_kinds():
    """Pure-function guard: the upload helper persists every remaining upload kind
    unchanged with an ``upload`` tag. Legacy portrait/broll are no longer UploadKind
    members (#133); the string-level normalizer that guards the non-upload ingestion
    paths is covered by ``test_normalize_visual_asset_kind_converges_legacy``."""
    from packages.core.contracts import UploadKind
    from apps.api.services.uploads import _visual_asset_kind_and_tags

    for passthrough in (
        UploadKind.video,
        UploadKind.bgm,
        UploadKind.font,
        UploadKind.image,
        UploadKind.cover_template,
    ):
        kind, tags = _visual_asset_kind_and_tags(passthrough)
        assert kind == passthrough.value
        assert tags == [passthrough.value, "upload"]


@pytest.mark.parametrize("legacy_kind", ["portrait", "broll"])
def test_normalize_visual_asset_kind_converges_legacy(legacy_kind):
    """Issue #133: the shared normalizer converges a legacy visual asset-kind string
    onto ``video`` with a ``legacy_kind:<x>`` provenance tag, so the import / create
    ingestion paths can never re-introduce a legacy asset-kind row after the enum
    removal. Non-legacy kinds pass through unchanged with no tag."""
    from packages.core.contracts import normalize_visual_asset_kind

    persisted, legacy_tag = normalize_visual_asset_kind(legacy_kind)
    assert persisted == "video"
    assert legacy_tag == f"legacy_kind:{legacy_kind}"
    for passthrough in ("video", "bgm", "font", "image", "cover_template", "other"):
        assert normalize_visual_asset_kind(passthrough) == (passthrough, None)


@pytest.mark.parametrize("legacy_kind", ["portrait", "broll"])
def test_legacy_visual_upload_kind_is_rejected(tmp_path, legacy_kind):
    """Issue #133: the legacy ``portrait`` / ``broll`` upload kinds were removed from
    ``UploadKind``, so ``prepare`` rejects them at request validation — they can no
    longer create a legacy asset-kind row via upload."""
    login_admin()
    _seed_case("case_legacy_visual")
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": legacy_kind,
            "case_id": "case_legacy_visual",
            "filename": f"{legacy_kind}.mp4",
            "content_type": "video/mp4",
            "size_bytes": 1024,
        },
    )
    assert prepared.status_code == 422, prepared.text


def test_video_upload_can_stabilize_before_creating_media_asset(tmp_path):
    require_ffmpeg_filters("vidstabdetect", "vidstabtransform")
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1.2, width=160, height=120, fps=15)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared, completed = direct_upload(
        client,
        kind="video",
        case_id="case_demo",
        filename="shaky.mp4",
        content_type="video/mp4",
        body=content,
        sha256=digest,
        stabilize=True,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["media_asset"]["source_artifact_id"] == body["artifact"]["artifact_id"]
    assert "stabilized" in body["media_asset"]["tags"]
    stabilized_artifact = _get_artifact(body["artifact"]["artifact_id"])
    assert stabilized_artifact.sha256 != digest
    assert (stabilized_artifact.payload or {})["stabilized"] is True
    info = probe_media(local_object_path(client.app.state.object_store, stabilized_artifact.uri))
    assert info.media_type == "video"
    assert 0.95 <= (info.duration_sec or 0) <= 1.45


def test_batch_stabilize_updates_media_assets_and_reports_results(tmp_path):
    require_ffmpeg_filters("vidstabdetect", "vidstabtransform")
    login_admin()
    _seed_case("case_batch_stabilize")
    video = generate_test_video(tmp_path, duration_sec=1.2, width=160, height=120, fps=15)
    content = video.read_bytes()
    prepared, completed = direct_upload(
        client,
        kind="video",
        case_id="case_batch_stabilize",
        filename="batch-shaky.mp4",
        content_type="video/mp4",
        body=content,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    asset_id = completed.json()["media_asset"]["id"]
    original_artifact_id = completed.json()["artifact"]["artifact_id"]

    response = client.post("/api/media/assets/batch-stabilize", json={"asset_ids": [asset_id]})

    assert response.status_code == 200, response.text
    body = response.json()
    updated_source_artifact_id, updated_tags = _media_asset_source_and_tags(asset_id)
    assert body["results"] == [
        {
            "asset_id": asset_id,
            "status": "completed",
            "artifact_id": updated_source_artifact_id,
            "error_code": None,
            "message": None,
        }
    ]
    assert updated_source_artifact_id != original_artifact_id
    assert "stabilized" in updated_tags


def test_annotation_trim_creates_trimmed_artifact_from_invalid_segments(tmp_path):
    login_admin()
    _seed_case("case_trim_annotation")
    video = generate_test_video(tmp_path, duration_sec=2, width=160, height=120, fps=15)
    content = video.read_bytes()
    prepared, completed = direct_upload(
        client,
        kind="video",
        case_id="case_trim_annotation",
        filename="trim-source.mp4",
        content_type="video/mp4",
        body=content,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    asset_id = completed.json()["media_asset"]["id"]
    editor = client.get(f"/api/annotations/{asset_id}").json()
    patched = client.patch(
        f"/api/annotations/{asset_id}",
        json={
            "etag": editor["etag"],
            "patch": {
                "operations": [
                    {
                        "op": "replace",
                        "path": "/projection/invalid_segments",
                        "value": [
                            {"start_sec": 0, "end_sec": 0.4, "reason": "开头无效"},
                            {"start_sec": 1.4, "end_sec": 2.0, "reason": "结尾无效"},
                        ],
                    }
                ]
            },
        },
    )
    assert patched.status_code == 200, patched.text

    response = client.post(f"/api/annotations/{asset_id}/trim", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    artifact = _get_artifact(body["artifact"]["artifact_id"])
    assert body["asset_id"] == asset_id
    assert 0.85 <= body["valid_duration_sec"] <= 1.15
    assert (artifact.payload or {})["trimmed"] is True
    trimmed_source_artifact_id, _ = _media_asset_source_and_tags(asset_id)
    assert trimmed_source_artifact_id == artifact.id
    info = probe_media(local_object_path(client.app.state.object_store, artifact.uri))
    assert 0.85 <= (info.duration_sec or 0) <= 1.15


def test_annotation_trim_rejects_empty_valid_region(tmp_path):
    login_admin()
    _seed_case("case_trim_empty")
    video = generate_test_video(tmp_path, duration_sec=1, width=160, height=120, fps=15)
    content = video.read_bytes()
    prepared, completed = direct_upload(
        client,
        kind="video",
        case_id="case_trim_empty",
        filename="all-invalid.mp4",
        content_type="video/mp4",
        body=content,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    asset_id = completed.json()["media_asset"]["id"]
    editor = client.get(f"/api/annotations/{asset_id}").json()
    patched = client.patch(
        f"/api/annotations/{asset_id}",
        json={
            "etag": editor["etag"],
            "patch": {
                "operations": [
                    {
                        "op": "replace",
                        "path": "/projection/invalid_segments",
                        "value": [{"start_sec": 0, "end_sec": 1, "reason": "全段无效"}],
                    }
                ]
            },
        },
    )
    assert patched.status_code == 200, patched.text

    response = client.post(f"/api/annotations/{asset_id}/trim", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "material.insufficient.broll"
