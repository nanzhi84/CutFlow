"""Regression: a failed complete() cleans up server-written derived objects.

normalize/stabilize each write a fresh object via store_file before a later step
may fail. The original _fail_upload only dropped the browser-written staging
object, leaking the normalized/stabilized derivative. This drives normalize to
succeed (writing a media-normalized object) and then forces stabilize to fail, and
asserts the derived object is gone after the failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.core.contracts import UploadKind
from packages.core.storage.database import CaseRow
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import FfmpegCommandError
from tests.api._upload_helpers import direct_upload
from tests.fixtures.media import generate_test_video, require_strict_bt709_tags

client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _seed_case(case_id: str) -> None:
    """Insert the shared case so the upload session satisfies the SQL FK. Idempotent."""
    with app.state.sqlalchemy_session_factory() as session:
        if session.get(CaseRow, case_id) is not None:
            return
        session.add(CaseRow(id=case_id, name="上传失败清理测试案例", status="active"))
        session.commit()


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


def test_failed_upload_cleans_up_normalized_derived_object(
    monkeypatch, upload_settings_override, tmp_path
):
    require_strict_bt709_tags()
    login_admin()
    _seed_case("case_fail_cleanup")
    upload_settings_override(normalize_video=True)

    # Stabilize runs AFTER normalize has already written a media-normalized object,
    # so forcing it to fail leaves a server-written derivative on disk mid-flight.
    normalized_path: Path | None = None

    def boom(video_path, *_args, **_kwargs):
        nonlocal normalized_path
        normalized_path = Path(video_path)
        assert normalized_path.exists()
        raise FfmpegCommandError("stabilize boom")

    monkeypatch.setattr("packages.media.upload_reconciler.stabilize_video", boom)

    video = generate_test_video(tmp_path, duration_sec=1, width=320, height=568, fps=15)
    content = video.read_bytes()
    prepared, completed = direct_upload(
        client,
        kind="video",
        filename="fail-cleanup.mp4",
        content_type="video/mp4",
        body=content,
        case_id="case_fail_cleanup",
        stabilize=True,
    )

    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 400, completed.text

    # Rejection is persisted before cleanup. Both the browser staging key and the
    # deterministic final key must be absent, and processing intermediates live in
    # an auto-cleaned temporary directory rather than beside the object-store path.
    session = client.get(
        f"/api/uploads/{prepared.json()['upload_session']['id']}"
    ).json()
    assert session["status"] == "rejected"
    staging_uri = session["staging_uri"]
    final_uri = app.state.upload_reconciler._final_uri_for(
        staging_uri,
        UploadKind(session["kind"]),
    )
    store = app.state.object_store
    assert not local_object_path(store, staging_uri).exists()
    assert not local_object_path(store, final_uri).exists()
    assert normalized_path is not None
    assert not normalized_path.exists()
