from fastapi.testclient import TestClient


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from packages.core.storage.database import ArtifactRow, VoiceProfileRow


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    return session_factory


def create_completed_voice_upload(client: TestClient) -> str:
    admin_login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert admin_login.status_code == 200, admin_login.text

    from tests.api._upload_helpers import direct_upload
    from tests.api.test_upload_object_store import _wav_bytes

    content = _wav_bytes(duration_sec=1)
    prepared, completed = direct_upload(
        client,
        kind="voice_reference",
        filename="voice.wav",
        content_type="audio/wav",
        body=content,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed is not None and completed.status_code == 200, (
        completed.text if completed is not None else prepared.text
    )
    return completed.json()["upload_session"]["id"]


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
            json={
                "display_name": "Cloned DB Voice",
                "reference_upload_session_id": reference_upload_id,
                "case_ids": ["case_demo"],
            },
        )
        assert cloned.status_code == 202, cloned.text
        cloned_voice = cloned.json()
        assert cloned_voice["source"] == "cloned"
        assert cloned_voice["case_ids"] == ["case_demo"]

        listed = client.get("/api/voices")
        assert listed.status_code == 200, listed.text
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert cloned_voice["id"] in listed_ids

        listed_by_case = client.get("/api/voices", params={"case_id": "case_demo"})
        assert listed_by_case.status_code == 200, listed_by_case.text
        assert cloned_voice["id"] in {item["id"] for item in listed_by_case.json()["items"]}

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
        assert all(
            item["source"] == "cloned" and item["enabled"] is False
            for item in filtered.json()["items"]
        )

        deleted = client.delete(f"/api/voices/{cloned_voice['id']}")
        assert deleted.status_code == 200, deleted.text

    with session_factory() as session:
        cloned_row = session.get(VoiceProfileRow, cloned_voice["id"])
        artifact_row = session.get(ArtifactRow, preview_body["audio_artifact"]["artifact_id"])
        assert cloned_row is None
        assert artifact_row is not None
        assert artifact_row.payload["text"] == "Hello from the database voice preview."
