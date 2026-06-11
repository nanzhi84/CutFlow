from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderResult
from packages.core.contracts import ArtifactKind, ProviderOptionsSchemaRef, ProviderProfile, UploadSession, UploadStatus, UploadKind
from packages.core.storage.repository import Repository
from packages.media.assets import store_file
from packages.media.video.ffmpeg import probe_media


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _profile(provider_id: str, capability: str, model_id: str) -> ProviderProfile:
    return ProviderProfile(
        id=f"{provider_id}.default",
        provider_id=provider_id,
        model_id=model_id,
        capability=capability,
        display_name=f"{provider_id} default",
        environment="local",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id=f"provider.{capability}.options"),
    )


def _payload(**overrides) -> dict:
    payload = {
        "case_id": "case_demo",
        "title": "Provider integration",
        "script": "真实 provider 产物必须进入流水线。",
        "voice": {"voice_id": "voice_sandbox"},
        "portrait": {"template_mode": "agent"},
        "broll": {"enabled": False},
        "bgm": {"enabled": False},
        "subtitle": {"enabled": True},
        "lipsync": {"enabled": False},
        "strictness": {"strict_timestamps": False},
    }
    payload.update(overrides)
    return payload


class ArtifactTTSProvider:
    provider_id = "fake.tts"

    def __init__(self, repository: Repository, object_store, source_audio) -> None:
        self.repository = repository
        self.object_store = object_store
        self.source_audio = source_audio
        self.artifact_id: str | None = None
        self.uri: str | None = None

    def invoke(self, call: ProviderCall) -> ProviderResult:
        stored = store_file(self.object_store, self.source_audio, purpose="fake-provider-audio")
        media_info = probe_media(self.source_audio)
        artifact = self.repository.create_artifact(
            kind=ArtifactKind.audio_tts,
            payload_schema="uri-only",
            payload=None,
            case_id=call.case_id,
            run_id=call.run_id,
            node_run_id=call.node_run_id,
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=media_info,
        )
        self.artifact_id = artifact.id
        self.uri = artifact.uri
        return ProviderResult(
            output={"audio_artifact_id": artifact.id, "audio_uri": artifact.uri},
            audio_seconds=float(media_info.duration_sec or 0),
        )


class ArtifactLipSyncProvider:
    provider_id = "fake.lipsync"

    def __init__(self, repository: Repository, object_store, source_video) -> None:
        self.repository = repository
        self.object_store = object_store
        self.source_video = source_video
        self.artifact_id: str | None = None
        self.uri: str | None = None

    def invoke(self, call: ProviderCall) -> ProviderResult:
        stored = store_file(self.object_store, self.source_video, purpose="fake-provider-video")
        media_info = probe_media(self.source_video)
        artifact = self.repository.create_artifact(
            kind=ArtifactKind.video_lipsync,
            payload_schema="uri-only",
            payload=None,
            case_id=call.case_id,
            run_id=call.run_id,
            node_run_id=call.node_run_id,
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=media_info,
        )
        self.artifact_id = artifact.id
        self.uri = artifact.uri
        return ProviderResult(
            output={"video_artifact_id": artifact.id, "video_uri": artifact.uri, "report": "pass"},
            video_seconds=float(media_info.duration_sec or 0),
        )


class FakeASRProvider:
    provider_id = "fake.asr"

    def invoke(self, call: ProviderCall) -> ProviderResult:
        return ProviderResult(
            output={
                "text": "真实 provider 产物必须进入流水线",
                "segments": [
                    {"start": 0.0, "end": 0.6, "text": "真实 provider"},
                    {"start": 0.6, "end": 1.2, "text": "产物必须进入流水线"},
                ],
                "source": "asr",
            },
            audio_seconds=1.2,
        )


class FakeVLMProvider:
    provider_id = "fake.vlm"

    def invoke(self, call: ProviderCall) -> ProviderResult:
        return ProviderResult(
            output={
                "canonical": {
                    "labels": ["provider-vlm", "usable-shot"],
                    "kind": "broll",
                    "quality": {"valid": True, "issues": []},
                    "scenes": [{"start": 0, "end": 1, "description": "provider scene"}],
                },
                "annotation_status": "annotated",
            },
            image_count=1,
        )


class FakeVoiceBuildProvider:
    provider_id = "fake.voice"

    def __init__(self, repository: Repository, object_store, source_audio) -> None:
        self.repository = repository
        self.object_store = object_store
        self.source_audio = source_audio
        self.operations: list[str] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        operation = str(call.input.get("operation") or "")
        self.operations.append(operation)
        stored = store_file(self.object_store, self.source_audio, purpose=f"fake-{operation}-preview")
        media_info = probe_media(self.source_audio)
        artifact = self.repository.create_artifact(
            kind=ArtifactKind.audio_tts,
            payload_schema="VoicePreviewArtifact.v1",
            payload={"operation": operation},
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=media_info,
        )
        return ProviderResult(
            output={
                "voice_id": f"voice_provider_{operation}",
                "preview_audio_artifact_id": artifact.id,
            }
        )


def test_tts_node_uses_provider_audio_artifact(media_fixture_factory):
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = ArtifactTTSProvider(
            repository,
            client.app.state.object_store,
            media_fixture_factory.audio(duration_sec=1.0, filename="provider-tts.wav"),
        )
        client.app.state.provider_gateway.register(provider)
        profile = _profile("fake.tts", "tts.speech", "fake-tts")
        repository.provider_profiles[profile.id] = profile

        response = client.post(
            "/api/jobs/digital-human-video",
            json=_payload(voice={"voice_id": "voice_sandbox", "provider_profile_id": profile.id}),
        )

        assert response.status_code == 201, response.text
        run_id = response.json()["initial_run"]["id"]
        tts_node = next(node for node in repository.node_runs[run_id] if node.node_id == "TTS")
        assert tts_node.output_artifact_ids == [provider.artifact_id]


def test_lipsync_node_uses_provider_video_artifact(media_fixture_factory):
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = ArtifactLipSyncProvider(
            repository,
            client.app.state.object_store,
            media_fixture_factory.video(duration_sec=1.0, filename="provider-lipsync.mp4"),
        )
        client.app.state.provider_gateway.register(provider)
        profile = _profile("fake.lipsync", "lipsync.video", "fake-lipsync")
        repository.provider_profiles[profile.id] = profile

        response = client.post(
            "/api/jobs/digital-human-video",
            json=_payload(
                lipsync={"enabled": True, "provider_profile_id": profile.id},
            ),
        )

        assert response.status_code == 201, response.text
        run_id = response.json()["initial_run"]["id"]
        lipsync_node = next(node for node in repository.node_runs[run_id] if node.node_id == "LipSync")
        assert provider.artifact_id in lipsync_node.output_artifact_ids


def test_strict_alignment_uses_available_asr_provider(media_fixture_factory):
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        client.app.state.provider_gateway.register(FakeASRProvider())
        profile = _profile("fake.asr", "asr.transcribe", "fake-asr")
        repository.provider_profiles[profile.id] = profile

        response = client.post(
            "/api/jobs/digital-human-video",
            json=_payload(strictness={"strict_timestamps": True}),
        )

        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        failed_nodes = [
            (node.node_id, node.error.code, node.error.message)
            for node in repository.node_runs[run["id"]]
            if node.error
        ]
        assert run["status"] == "succeeded", failed_nodes
        alignment_node = next(node for node in repository.node_runs[run["id"]] if node.node_id == "NarrationAlignment")
        artifacts = [repository.artifacts[artifact_id] for artifact_id in alignment_node.output_artifact_ids]
        narration = next(artifact for artifact in artifacts if artifact.kind == ArtifactKind.narration_units)
        assert narration.payload["source"] == "asr"
        assert narration.payload["strict"] is True
        assert narration.payload["warnings"] == []


def test_annotation_rerun_uses_vlm_provider_output():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        client.app.state.provider_gateway.register(FakeVLMProvider())
        profile = _profile("fake.vlm", "vlm.annotation", "fake-vlm")
        repository.provider_profiles[profile.id] = profile
        asset_id = "asset_broll_demo"
        repository.media_assets[asset_id] = repository.media_assets[asset_id].model_copy(
            update={"annotation_status": "pending", "usable": True}
        )

        rerun = client.post(
            f"/api/annotations/{asset_id}/rerun",
            json={"provider_profile_id": profile.id, "force": True},
        )

        assert rerun.status_code == 202, rerun.text
        assert rerun.json()["status"] == "completed"
        editor = client.get(f"/api/annotations/{asset_id}")
        assert editor.status_code == 200, editor.text
        assert editor.json()["canonical"]["labels"] == ["provider-vlm", "usable-shot"]
        assert repository.media_assets[asset_id].annotation_status == "annotated"


def test_voice_preview_uses_tts_provider_artifact(media_fixture_factory):
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = ArtifactTTSProvider(
            repository,
            client.app.state.object_store,
            media_fixture_factory.audio(duration_sec=1.0, filename="voice-preview.wav"),
        )
        client.app.state.provider_gateway.register(provider)
        profile = _profile("fake.tts", "tts.speech", "fake-tts")
        repository.provider_profiles[profile.id] = profile

        response = client.post(
            "/api/voices/voice_sandbox/preview",
            json={"text": "试听真实 provider", "provider_profile_id": profile.id},
        )

        assert response.status_code == 200, response.text
        assert response.json()["audio_artifact"]["artifact_id"] == provider.artifact_id
        assert repository.voices["voice_sandbox"].preview_artifact_id == provider.artifact_id


def test_voice_design_uses_provider_voice_id_and_preview(media_fixture_factory):
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = FakeVoiceBuildProvider(
            repository,
            client.app.state.object_store,
            media_fixture_factory.audio(duration_sec=1.0, filename="voice-design-preview.wav"),
        )
        client.app.state.provider_gateway.register(provider)
        profile = _profile("fake.voice", "tts.speech", "fake-voice")
        repository.provider_profiles[profile.id] = profile

        response = client.post(
            "/api/voices/design",
            json={"display_name": "Provider Design", "prompt": "calm", "provider_profile_id": profile.id},
        )

        assert response.status_code == 202, response.text
        assert response.json()["id"] == "voice_provider_design"
        assert response.json()["preview_artifact_id"]
        assert provider.operations == ["design"]


def test_voice_clone_uses_provider_voice_id_and_preview(media_fixture_factory):
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = FakeVoiceBuildProvider(
            repository,
            client.app.state.object_store,
            media_fixture_factory.audio(duration_sec=1.0, filename="voice-clone-preview.wav"),
        )
        client.app.state.provider_gateway.register(provider)
        profile = _profile("fake.voice", "tts.speech", "fake-voice")
        repository.provider_profiles[profile.id] = profile
        repository.uploads["upl_voice_ref"] = UploadSession(
            id="upl_voice_ref",
            kind=UploadKind.voice_reference,
            filename="voice-ref.wav",
            content_type="audio/wav",
            size_bytes=100,
            status=UploadStatus.completed,
        )

        response = client.post(
            "/api/voices/clone",
            json={
                "display_name": "Provider Clone",
                "reference_upload_session_id": "upl_voice_ref",
                "provider_profile_id": profile.id,
            },
        )

        assert response.status_code == 202, response.text
        assert response.json()["id"] == "voice_provider_clone"
        assert response.json()["preview_artifact_id"]
        assert provider.operations == ["clone"]
