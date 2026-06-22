import hashlib
import os

import pytest
from fastapi.testclient import TestClient

if os.getenv("CUTAGENT_RUN_DB_TESTS") != "1":
    pytest.skip("Set CUTAGENT_RUN_DB_TESTS=1 to run database integration tests.", allow_module_level=True)

from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import ArtifactRow, VoiceProfileRow


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def create_completed_voice_upload(client: TestClient) -> str:
    admin_login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert admin_login.status_code == 200, admin_login.text

    import tempfile
    from pathlib import Path

    from tests.fixtures.media import generate_test_audio

    with tempfile.TemporaryDirectory() as fixture_dir:
        content = generate_test_audio(Path(fixture_dir), duration_sec=1).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "voice_reference",
            "filename": "voice.wav",
            "content_type": "audio/wav",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("voice.wav", content, "audio/wav")},
    )
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
    assert completed.status_code == 200, completed.text
    return upload["id"]


def test_sqlalchemy_voice_profile_flow_persists_profiles_and_preview_artifact():
    session_factory = sqlalchemy_session_factory()

    with TestClient(app) as client:
        reference_upload_id = create_completed_voice_upload(client)
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        cloned = client.post(
            "/api/voices/clone",
            json={"display_name": "Cloned DB Voice", "reference_upload_session_id": reference_upload_id},
        )
        assert cloned.status_code == 202, cloned.text
        cloned_voice = cloned.json()
        assert cloned_voice["source"] == "cloned"

        listed = client.get("/api/voices")
        assert listed.status_code == 200, listed.text
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert cloned_voice["id"] in listed_ids

        preview = client.post(
            f"/api/voices/{cloned_voice['id']}/preview",
            json={"text": "Hello from the database voice preview."},
        )
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        assert preview_body["audio_artifact"]["kind"] == "audio.tts"

        patched = client.patch(
            f"/api/voices/{cloned_voice['id']}",
            json={"display_name": "Disabled DB Voice", "enabled": False},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["enabled"] is False

        filtered = client.get("/api/voices", params={"source": "cloned", "enabled": False})
        assert filtered.status_code == 200, filtered.text
        assert any(item["id"] == cloned_voice["id"] for item in filtered.json()["items"])
        assert all(item["source"] == "cloned" and item["enabled"] is False for item in filtered.json()["items"])

        deleted = client.delete(f"/api/voices/{designed_voice['id']}")
        assert deleted.status_code == 200, deleted.text

    with session_factory() as session:
        cloned_row = session.get(VoiceProfileRow, cloned_voice["id"])
        designed_row = session.get(VoiceProfileRow, designed_voice["id"])
        artifact_row = session.get(ArtifactRow, preview_body["audio_artifact"]["artifact_id"])
        assert cloned_row is not None
        assert cloned_row.display_name == "Disabled DB Voice"
        assert cloned_row.enabled is False
        assert designed_row is None
        assert artifact_row is not None
        assert artifact_row.payload["text"] == "Hello from the database voice preview."
