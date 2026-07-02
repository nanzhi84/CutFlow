"""Regression: a failed complete() cleans up server-written derived objects.

normalize/stabilize each write a fresh object via store_file before a later step
may fail. The original _fail_upload only dropped the browser-written staging
object, leaking the normalized/stabilized derivative. This drives normalize to
succeed (writing a media-normalized object) and then forces stabilize to fail, and
asserts the derived object is gone after the failure.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
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

    def apply(**upload_overrides):
        new_upload = original.upload.model_copy(update=upload_overrides)
        app.state.settings = original.model_copy(update={"upload": new_upload})

    yield apply
    app.state.settings = original


def test_failed_upload_cleans_up_normalized_derived_object(
    monkeypatch, upload_settings_override, tmp_path
):
    require_strict_bt709_tags()
    login_admin()
    _seed_case("case_fail_cleanup")
    upload_settings_override(normalize_video=True)

    # Stabilize runs AFTER normalize has already written a media-normalized object,
    # so forcing it to fail leaves a server-written derivative on disk mid-flight.
    def boom(*args, **kwargs):
        raise FfmpegCommandError("stabilize boom")

    monkeypatch.setattr("apps.api.services.uploads.stabilize_video", boom)

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

    # object_uri still points at the normalized derivative (the last successful
    # patch before stabilize failed); _fail_upload must have deleted that object.
    session = client.get(
        f"/api/uploads/{prepared.json()['upload_session']['id']}"
    ).json()
    assert session["status"] == "failed"
    derived_uri = session["object_uri"]
    assert "media-normalized" in derived_uri, derived_uri
    store = app.state.object_store
    assert not local_object_path(store, derived_uri).exists()


def test_fail_upload_best_effort_deletes_staging_and_all_derived_objects(monkeypatch):
    """Deterministic guard for the C5 cleanup contract (no ffmpeg / DB needed, so it
    runs in every environment regardless of the normalize-path media gating above).

    The original _fail_upload only dropped ``staging_uri``; the fix must also drop
    every server-written derived object. Pre-fix this test is red (the derived URIs
    never reach _safe_delete because the parameter did not exist)."""
    from apps.api.services import uploads

    deleted: list[str] = []
    monkeypatch.setattr(uploads, "_safe_delete", lambda store, uri: deleted.append(uri))
    monkeypatch.setattr(uploads, "_patch_upload", lambda *a, **k: None)

    uploads._fail_upload(
        request=object(),
        store=object(),
        upload_id="up_c5",
        staging_uri="local://incoming/uploads/up_c5",
        derived_uris={
            "local://materials/media-normalized/up_c5.mp4",
            "local://materials/media-stabilized/up_c5.mp4",
        },
    )

    assert "local://incoming/uploads/up_c5" in deleted  # staging always dropped
    assert "local://materials/media-normalized/up_c5.mp4" in deleted  # derived
    assert "local://materials/media-stabilized/up_c5.mp4" in deleted  # derived
    assert len(deleted) == 3
