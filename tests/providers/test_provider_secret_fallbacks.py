from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core.contracts import ArtifactKind, ArtifactRef, VoicePreviewRequest, VoicePreviewResponse, VoiceProfile
from tests.providers.test_sqlalchemy_voice_provider_wireup import (
    RecordingSqlAlchemyVoiceRepository,
    RecordingTTSProvider,
    _login_admin,
    _profile,
)


class FallbackSqlAlchemyVoiceRepository(RecordingSqlAlchemyVoiceRepository):
    def preview_voice(self, voice_id: str, payload: VoicePreviewRequest) -> VoicePreviewResponse | None:
        self.preview_called = True
        return VoicePreviewResponse(
            voice_id=voice_id,
            audio_artifact=ArtifactRef(
                artifact_id="art_sandbox_preview",
                kind=ArtifactKind.audio_tts,
                uri=f"sandbox://voice-preview/{voice_id}.wav",
            ),
            duration_sec=1.0,
        )


def test_sqlalchemy_voice_preview_falls_back_to_sandbox_when_secret_is_missing():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        media_repository = FallbackSqlAlchemyVoiceRepository()
        provider = RecordingTTSProvider(repository)
        profile = _profile("fake.tts", "tts.speech", "fake-tts").model_copy(update={"secret_ref": "missing.secret"})
        media_repository.voices["voice_db"] = media_repository.voices["voice_db"].model_copy(
            update={"provider_profile_id": profile.id}
        )
        client.app.state.sqlalchemy_media_repository = media_repository
        client.app.state.provider_gateway.register(provider)
        repository.provider_profiles[profile.id] = profile

        response = client.post("/api/voices/voice_db/preview", json={"text": "fallback preview"})

        assert response.status_code == 200, response.text
        assert response.json()["audio_artifact"]["uri"].startswith("sandbox://")
        assert provider.calls == []
        assert media_repository.preview_called is True


def test_tts_node_falls_back_to_sandbox_when_voice_profile_secret_is_missing():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = RecordingTTSProvider(repository)
        profile = _profile("fake.tts", "tts.speech", "fake-tts").model_copy(update={"secret_ref": "missing.secret"})
        client.app.state.provider_gateway.register(provider)
        repository.provider_profiles[profile.id] = profile
        repository.voices["voice_secret"] = VoiceProfile(
            id="voice_secret",
            display_name="Secret Voice",
            source="builtin",
            provider_profile_id=profile.id,
        )

        response = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "Missing secret fallback",
                "script": "缺少 secret 时仍应走 sandbox。",
                "voice": {"voice_id": "voice_secret"},
                "broll": {"enabled": False},
                "bgm": {"enabled": False},
                "subtitle": {"enabled": False},
                "lipsync": {"enabled": False},
                "strictness": {"strict_timestamps": False},
            },
        )

        assert response.status_code == 201, response.text
        assert response.json()["initial_run"]["status"] == "succeeded"
        assert provider.calls == []


def test_video_job_fails_loudly_when_sandbox_fallback_disabled(monkeypatch):
    monkeypatch.setenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "0")
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = RecordingTTSProvider(repository)
        profile = _profile("fake.tts", "tts.speech", "fake-tts").model_copy(update={"secret_ref": "missing.secret"})
        client.app.state.provider_gateway.register(provider)
        repository.provider_profiles[profile.id] = profile
        repository.voices["voice_secret"] = VoiceProfile(
            id="voice_secret",
            display_name="Secret Voice",
            source="builtin",
            provider_profile_id=profile.id,
        )

        response = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "No sandbox fallback",
                "script": "无真实供应商时不应兜底到 sandbox。",
                "voice": {"voice_id": "voice_secret"},
                "broll": {"enabled": False},
                "bgm": {"enabled": False},
                "subtitle": {"enabled": True},
                "lipsync": {"enabled": False},
                "strictness": {"strict_timestamps": False},
            },
        )

        assert provider.calls == []
        if response.status_code == 201:
            assert response.json()["initial_run"]["status"] != "succeeded"
        else:
            assert response.status_code >= 400, response.text
