import hashlib
import io
import math
import struct
import wave

from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.main import repository
from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    FinishedVideo,
    Job,
    JobType,
    MediaInfo,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.repository import new_id
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import probe_media
from tests.fixtures.media import generate_test_video, require_ffmpeg_filters


client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


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
    content = b"cutagent object store"
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "portrait",
            "case_id": "case_stabilize_upload",
            "filename": "sample.txt",
            "content_type": "text/plain",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    assert upload["object_uri"].startswith("local://")
    assert upload["upload_url"].startswith("local://")

    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("sample.txt", content, "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["status"] == "uploading"

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
    assert completed.status_code == 200, completed.text
    artifact = completed.json()["artifact"]
    assert artifact["kind"] == "uploaded.file"
    assert completed.json()["media_asset"]["kind"] == "portrait"
    assert completed.json()["media_asset"]["source_artifact_id"] == artifact["artifact_id"]


def test_create_media_asset_from_completed_upload_points_to_uploaded_artifact():
    login_admin()
    content = _wav_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "bgm",
            "case_id": "case_demo",
            "filename": "asset-create.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("asset-create.wav", content, "audio/wav")},
    )
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
    assert completed.status_code == 200, completed.text
    artifact_id = completed.json()["artifact"]["artifact_id"]

    created = client.post(
        "/api/media/assets",
        json={
            "upload_session_id": upload["id"],
            "case_id": "case_demo",
            "title": "Created from upload",
            "kind": "bgm",
            "tags": ["bgm"],
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["source_artifact_id"] == artifact_id
    assert created.json()["source_artifact_id"] in repository().artifacts


def test_local_media_preview_url_is_browser_playable_content_route():
    login_admin()
    content = _wav_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "bgm",
            "filename": "preview.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()

    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("preview.wav", content, "audio/wav")},
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
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
    repo = repository()
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
    repo = repository()
    # A dedicated case so these (deliberately non-downloadable) finished videos
    # never leak into case_demo, whose last finished video other suites publish.
    case_id = "case_stream_proxy_test"
    run = WorkflowRun(
        id=new_id("run"),
        job_id=new_id("job"),
        case_id=case_id,
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.succeeded,
    )
    repo.runs[run.id] = run
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


def test_upload_file_rejects_size_mismatch_before_completion():
    login_admin()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "broll",
            "filename": "short.txt",
            "content_type": "text/plain",
            "size_bytes": 100,
        },
    ).json()
    response = client.put(
        f"/api/uploads/{prepared['id']}/file",
        files={"file": ("short.txt", b"short", "text/plain")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "upload.size_mismatch"


def test_publish_video_upload_creates_publish_package(tmp_path):
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568)
    content = video.read_bytes()
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
        json={
            "upload_session_id": upload["id"],
            "sha256": digest,
            "metadata": {"title": "Publish upload"},
        },
    )

    assert completed.status_code == 200, completed.text
    assert completed.json()["media_asset"] is None
    assert completed.json()["publish_package"]["upload_artifact_id"] == completed.json()["artifact"]["artifact_id"]
    assert completed.json()["publish_package"]["platform_defaults"]["title"] == "Publish upload"


def test_video_upload_probes_media_and_creates_real_thumbnail_artifacts(tmp_path):
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "portrait",
            "case_id": "case_demo",
            "filename": "portrait.mp4",
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("portrait.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )

    assert completed.status_code == 200, completed.text
    artifact_id = completed.json()["artifact"]["artifact_id"]
    uploaded_artifact = repository().artifacts[artifact_id]
    assert uploaded_artifact.sha256 == digest
    assert uploaded_artifact.media_info is not None
    assert uploaded_artifact.media_info.media_type == "video"
    assert uploaded_artifact.media_info.width == 320
    thumbnails = [
        artifact
        for artifact in repository().artifacts.values()
        if artifact.kind == ArtifactKind.cover_image
        and (artifact.payload or {}).get("source_artifact_id") == artifact_id
    ]
    assert {artifact.payload["thumbnail_label"] for artifact in thumbnails} == {"first", "mid"}
    assert all(artifact.sha256 for artifact in thumbnails)
    assert all(artifact.media_info and artifact.media_info.media_type == "image" for artifact in thumbnails)


def test_unified_video_kind_upload_creates_video_media_asset(tmp_path):
    """P0: the unified ``video`` bucket creates a media asset (kind=video) through the
    same probe/normalize path as portrait/broll — the operator never picks A/B-roll."""
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "video",
            "case_id": "case_unified_video",
            "filename": "mixed.mp4",
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    assert upload["kind"] == "video"
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("mixed.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["media_asset"] is not None
    assert body["media_asset"]["kind"] == "video"
    assert body["media_asset"]["source_artifact_id"] == body["artifact"]["artifact_id"]
    artifact = repository().artifacts[body["artifact"]["artifact_id"]]
    assert artifact.media_info is not None and artifact.media_info.media_type == "video"


def test_video_upload_can_stabilize_before_creating_media_asset(tmp_path):
    require_ffmpeg_filters("vidstabdetect", "vidstabtransform")
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1.2, width=160, height=120, fps=15)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "portrait",
            "case_id": "case_demo",
            "filename": "shaky.mp4",
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
            "stabilize": True,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("shaky.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["media_asset"]["source_artifact_id"] == body["artifact"]["artifact_id"]
    assert "stabilized" in body["media_asset"]["tags"]
    stabilized_artifact = repository().artifacts[body["artifact"]["artifact_id"]]
    assert stabilized_artifact.sha256 != digest
    assert (stabilized_artifact.payload or {})["stabilized"] is True
    info = probe_media(local_object_path(client.app.state.object_store, stabilized_artifact.uri))
    assert info.media_type == "video"
    assert 0.95 <= (info.duration_sec or 0) <= 1.45


def test_batch_stabilize_updates_media_assets_and_reports_results(tmp_path):
    require_ffmpeg_filters("vidstabdetect", "vidstabtransform")
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1.2, width=160, height=120, fps=15)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "broll",
            "case_id": "case_batch_stabilize",
            "filename": "batch-shaky.mp4",
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("batch-shaky.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
    assert completed.status_code == 200, completed.text
    asset_id = completed.json()["media_asset"]["id"]
    original_artifact_id = completed.json()["artifact"]["artifact_id"]

    response = client.post("/api/media/assets/batch-stabilize", json={"asset_ids": [asset_id]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"] == [
        {
            "asset_id": asset_id,
            "status": "completed",
            "artifact_id": repository().media_assets[asset_id].source_artifact_id,
            "error_code": None,
            "message": None,
        }
    ]
    updated_asset = repository().media_assets[asset_id]
    assert updated_asset.source_artifact_id != original_artifact_id
    assert "stabilized" in updated_asset.tags


def test_annotation_trim_creates_trimmed_artifact_from_invalid_segments(tmp_path):
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=2, width=160, height=120, fps=15)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "broll",
            "case_id": "case_trim_annotation",
            "filename": "trim-source.mp4",
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("trim-source.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
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
    artifact = repository().artifacts[body["artifact"]["artifact_id"]]
    assert body["asset_id"] == asset_id
    assert 0.85 <= body["valid_duration_sec"] <= 1.15
    assert (artifact.payload or {})["trimmed"] is True
    assert repository().media_assets[asset_id].source_artifact_id == artifact.id
    info = probe_media(local_object_path(client.app.state.object_store, artifact.uri))
    assert 0.85 <= (info.duration_sec or 0) <= 1.15


def test_annotation_trim_rejects_empty_valid_region(tmp_path):
    login_admin()
    video = generate_test_video(tmp_path, duration_sec=1, width=160, height=120, fps=15)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "broll",
            "case_id": "case_trim_empty",
            "filename": "all-invalid.mp4",
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("all-invalid.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
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
