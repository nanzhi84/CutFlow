from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.core.contracts import Artifact
from packages.core.storage.database import ArtifactRow, CaseRow
from packages.core.storage.object_store import parse_local_uri, parse_object_uri
from packages.core.storage.sqlalchemy_uploads import artifact_row_to_contract
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media
from tests.fixtures.media import (
    generate_test_hdr_video,
    generate_test_video,
    require_ffmpeg_filters,
    require_strict_bt709_tags,
)
from tests.api._upload_helpers import direct_upload, minimal_ttf_bytes


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
    original_reconciler_settings = app.state.upload_reconciler.settings

    def apply(**upload_overrides):
        new_upload = original.upload.model_copy(update=upload_overrides)
        app.state.settings = original.model_copy(update={"upload": new_upload})
        app.state.upload_reconciler.settings = new_upload

    yield apply
    app.state.settings = original
    app.state.upload_reconciler.settings = original_reconciler_settings


def test_direct_upload_round_trips_large_payload_byte_for_byte():
    login_admin()
    _seed_case("case_stream")
    # The browser PUTs straight to OSS; complete verifies + finalizes. A large
    # payload must round-trip byte-for-byte and keep its sha256 (there is no
    # server proxy mutating bytes anymore). Use the incidental font kind so
    # complete skips ffprobe and does not auto-create a MediaAsset.
    content = minimal_ttf_bytes(family="Large Upload") + b"streamed-upload-" * 5000
    digest = hashlib.sha256(content).hexdigest()
    prepared, completed = direct_upload(
        client,
        kind="font",
        filename="big.ttf",
        content_type="font/ttf",
        body=content,
        case_id="case_stream",
        metadata={"template_mode": "replace"},
    )

    assert completed.status_code == 200, completed.text
    session = completed.json()["upload_session"]
    # object_uri is the FINAL key after complete copied staging -> final.
    stored = client.app.state.object_store.get_bytes(parse_object_uri(session["object_uri"]))
    assert stored == content
    assert hashlib.sha256(stored).hexdigest() == digest
    # complete recomputes sha256 from the downloaded object; it must match.
    assert session["sha256"] == digest


def test_complete_rejects_size_mismatch_via_head_verification():
    login_admin()
    _seed_case("case_stream")
    # The old byte proxy aborted mid-stream with 413 once it crossed a size
    # ceiling. With browser-direct upload the API never sees the bytes, so size
    # integrity is enforced at complete: it HEADs the stored object and rejects
    # when the actual size disagrees with the declared size. Incidental font kind
    # avoids ffprobe; the size check fires before the sha256 check.
    body = b"x" * 4096
    digest = hashlib.sha256(body).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "client_upload_id": "client_stream_size_mismatch",
            "kind": "font",
            "case_id": "case_stream",
            "filename": "size.ttf",
            "content_type": "font/ttf",
            "size_bytes": 16,
            "sha256": digest,
            "stabilize": False,
        },
    )
    assert prepared.status_code == 201, prepared.text
    # Browser PUTs 4096 bytes, but complete declares 16 -> HEAD size check fails.
    client.app.state.object_store.put_bytes(parse_local_uri(prepared.json()["put_url"]), body)
    response = client.post(
        "/api/uploads/complete",
        json={
            "upload_session_id": prepared.json()["upload_session"]["id"],
            "size_bytes": 16,
            "sha256": digest,
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "upload.size_mismatch"


def test_complete_upload_normalizes_portrait_when_enabled(upload_settings_override, tmp_path):
    require_strict_bt709_tags()
    login_admin()
    _seed_case("case_stream")
    upload_settings_override(normalize_video=True)
    # Odd-resolution SDR portrait that must be normalized to 1080x1920 bt709.
    video = generate_test_video(tmp_path, duration_sec=1, width=300, height=540, fps=15)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared, completed = direct_upload(
        client,
        kind="video",
        filename="raw.mp4",
        content_type="video/mp4",
        body=content,
        case_id="case_stream",
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert "normalized" in body["media_asset"]["tags"]
    assert body["upload_session"]["normalized"] is True
    artifact = _get_artifact(body["artifact"]["artifact_id"])
    # The admitted asset is the normalized one (sha changed) and conforms to profile.
    assert artifact.sha256 != digest
    info = probe_media(local_object_path(client.app.state.object_store, artifact.uri))
    assert (info.width, info.height) == (1080, 1920)
    assert info.color_transfer == "bt709"
    assert info.is_hdr is False


def test_complete_upload_skips_normalization_when_disabled(tmp_path):
    login_admin()
    _seed_case("case_stream")
    # Default settings: normalize_video is off, so the source passes through.
    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568, fps=15)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared, completed = direct_upload(
        client,
        kind="video",
        filename="passthrough.mp4",
        content_type="video/mp4",
        body=content,
        case_id="case_stream",
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert "normalized" not in body["media_asset"]["tags"]
    artifact = _get_artifact(body["artifact"]["artifact_id"])
    # Untouched: same bytes, original 320x568 dimensions.
    assert artifact.sha256 == digest
    info = probe_media(local_object_path(client.app.state.object_store, artifact.uri))
    assert (info.width, info.height) == (320, 568)


def test_complete_upload_normalizes_hdr_portrait_to_bt709_when_enabled(
    upload_settings_override, tmp_path
):
    require_ffmpeg_filters("zscale")
    require_strict_bt709_tags()
    login_admin()
    _seed_case("case_stream")
    upload_settings_override(normalize_video=True)
    try:
        video = generate_test_hdr_video(tmp_path, duration_sec=1, width=320, height=568, fps=15)
    except FfmpegCommandError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"HDR fixture unavailable: {exc}")
    if not probe_media(video).is_hdr:  # pragma: no cover - environment-dependent
        pytest.skip("ffmpeg did not tag the fixture as HDR")
    content = video.read_bytes()
    prepared, completed = direct_upload(
        client,
        kind="video",
        filename="hdr.mp4",
        content_type="video/mp4",
        body=content,
        case_id="case_stream",
    )

    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert "normalized" in body["media_asset"]["tags"]
    assert body["upload_session"]["normalized"] is True
    artifact = _get_artifact(body["artifact"]["artifact_id"])
    info = probe_media(local_object_path(client.app.state.object_store, artifact.uri))
    # HDR source admitted as BT.709 SDR — no silent color degrade downstream.
    assert info.is_hdr is False
    assert info.color_transfer == "bt709"
    assert (info.width, info.height) == (1080, 1920)
