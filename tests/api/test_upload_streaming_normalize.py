from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.core.contracts import Artifact
from packages.core.storage.database import ArtifactRow, CaseRow
from packages.core.storage.object_store import parse_object_uri
from packages.core.storage.sqlalchemy_uploads import artifact_row_to_contract
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media
from tests.fixtures.media import (
    generate_test_hdr_video,
    generate_test_video,
    require_ffmpeg_filters,
    require_strict_bt709_tags,
)


client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _seed_case(case_id: str) -> None:
    """Insert the shared case so upload sessions satisfy the FK the SQL backend
    enforces (the removed in-memory backend did not). Idempotent."""
    with app.state.sqlalchemy_session_factory() as session:
        if session.get(CaseRow, case_id) is not None:
            return
        session.add(CaseRow(id=case_id, name="流式上传测试案例", status="active"))
        session.commit()


def _get_artifact(artifact_id: str) -> Artifact:
    """Read a persisted artifact back from Postgres (the API writes through to SQL)."""
    with app.state.sqlalchemy_session_factory() as session:
        row = session.get(ArtifactRow, artifact_id)
        assert row is not None, f"artifact {artifact_id} not persisted"
        return artifact_row_to_contract(row)


@pytest.fixture
def upload_settings_override():
    """Temporarily override settings.upload on the live app state, then restore."""
    original = app.state.settings

    def apply(**upload_overrides):
        new_upload = original.upload.model_copy(update=upload_overrides)
        app.state.settings = original.model_copy(update={"upload": new_upload})

    yield apply
    app.state.settings = original


def _prepare(kind: str, content: bytes, *, content_type: str, filename: str, stabilize: bool = False):
    _seed_case("case_stream")
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": kind,
            "case_id": "case_stream",
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(content),
            "sha256": digest,
            "stabilize": stabilize,
        },
    )
    assert prepared.status_code == 201, prepared.text
    return prepared.json(), digest


def test_streaming_upload_chunked_path_round_trips_large_payload(upload_settings_override):
    login_admin()
    # Force a tiny streaming chunk so we exercise the multi-iteration read loop.
    upload_settings_override(chunk_bytes=64)
    content = b"streamed-upload-" * 5000  # ~75 KiB, many 64-byte chunks
    upload, digest = _prepare("portrait", content, content_type="text/plain", filename="big.txt")

    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("big.txt", content, "text/plain")},
    )

    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["status"] == "uploading"
    # The body landed in the object store byte-for-byte via the streamed path.
    stored = client.app.state.object_store.get_bytes(parse_object_uri(upload["object_uri"]))
    assert stored == content
    assert hashlib.sha256(stored).hexdigest() == digest


def test_upload_rejects_oversize_body_with_413(upload_settings_override):
    login_admin()
    # Hard ceiling below the declared size: the stream must abort early.
    upload_settings_override(max_size_bytes=16, chunk_bytes=8)
    content = b"x" * 4096
    upload, _ = _prepare("broll", content, content_type="text/plain", filename="oversize.txt")

    response = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("oversize.txt", content, "text/plain")},
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "upload.too_large"


def test_complete_upload_normalizes_portrait_when_enabled(upload_settings_override, tmp_path):
    require_strict_bt709_tags()
    login_admin()
    upload_settings_override(normalize_video=True)
    # Odd-resolution SDR portrait that must be normalized to 1080x1920 bt709.
    video = generate_test_video(tmp_path, duration_sec=1, width=300, height=540, fps=15)
    content = video.read_bytes()
    upload, digest = _prepare("portrait", content, content_type="video/mp4", filename="raw.mp4")
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("raw.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert "normalized" in body["media_asset"]["tags"]
    artifact = _get_artifact(body["artifact"]["artifact_id"])
    # The admitted asset is the normalized one (sha changed) and conforms to profile.
    assert artifact.sha256 != digest
    info = probe_media(local_object_path(client.app.state.object_store, artifact.uri))
    assert (info.width, info.height) == (1080, 1920)
    assert info.color_transfer == "bt709"
    assert info.is_hdr is False


def test_complete_upload_skips_normalization_when_disabled(tmp_path):
    login_admin()
    # Default settings: normalize_video is off, so the source passes through.
    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568, fps=15)
    content = video.read_bytes()
    upload, digest = _prepare("portrait", content, content_type="video/mp4", filename="passthrough.mp4")
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("passthrough.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert "normalized" not in body["media_asset"]["tags"]
    artifact = _get_artifact(body["artifact"]["artifact_id"])
    # Untouched: same bytes, original 320x568 dimensions.
    assert artifact.sha256 == digest
    info = probe_media(local_object_path(client.app.state.object_store, artifact.uri))
    assert (info.width, info.height) == (320, 568)


def test_complete_upload_normalizes_hdr_portrait_to_bt709_when_enabled(upload_settings_override, tmp_path):
    require_ffmpeg_filters("zscale")
    require_strict_bt709_tags()
    login_admin()
    upload_settings_override(normalize_video=True)
    try:
        video = generate_test_hdr_video(tmp_path, duration_sec=1, width=320, height=568, fps=15)
    except FfmpegCommandError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"HDR fixture unavailable: {exc}")
    if not probe_media(video).is_hdr:  # pragma: no cover - environment-dependent
        pytest.skip("ffmpeg did not tag the fixture as HDR")
    content = video.read_bytes()
    upload, digest = _prepare("portrait", content, content_type="video/mp4", filename="hdr.mp4")
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("hdr.mp4", content, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert "normalized" in body["media_asset"]["tags"]
    artifact = _get_artifact(body["artifact"]["artifact_id"])
    info = probe_media(local_object_path(client.app.state.object_store, artifact.uri))
    # HDR source admitted as BT.709 SDR — no silent color degrade downstream.
    assert info.is_hdr is False
    assert info.color_transfer == "bt709"
    assert (info.width, info.height) == (1080, 1920)
