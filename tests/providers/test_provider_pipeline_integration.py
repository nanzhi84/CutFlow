from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderResult
from sqlalchemy import select

from packages.core.contracts import (
    ArtifactKind,
    CreateProviderProfileRequest,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    UploadSession,
    UploadStatus,
    UploadKind,
)
from packages.core.storage.database import ArtifactRow, MediaAssetRow
from packages.core.storage.repository import Repository
from packages.media.annotation import bgm as bgm_annotation
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


def _arm_profile(active_client, provider_id: str, capability: str, model_id: str) -> ProviderProfile:
    """Persist a real provider profile in Postgres and return it.

    Capability-based resolution (``first_available`` / ``list_profiles`` for ASR,
    VLM, audio.understanding, voice clone) reads the gateway's SQL provider_reader,
    not the in-memory run-state repository, so these profiles must live in the DB.
    """
    return active_client.app.state.sqlalchemy_provider_repository.create_profile(
        CreateProviderProfileRequest(
            provider_id=provider_id,
            model_id=model_id,
            capability=capability,
            display_name=f"{provider_id} default",
            environment="local",
            options_schema_ref=ProviderOptionsSchemaRef(schema_id=f"provider.{capability}.options"),
        )
    )


def _payload(**overrides) -> dict:
    payload = {
        "case_id": "case_demo",
        "title": "Provider integration",
        "script": "真实 provider 产物必须进入流水线。",
        "voice": {"voice_id": "voice_sandbox"},
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
        self.calls: list[ProviderCall] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
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

    def __init__(self, upload_repo, object_store, source_audio) -> None:
        # ``upload_repo`` is the SQLAlchemy upload repository: the preview artifact
        # must be persisted in Postgres because the cloned voice row carries a FK to
        # ``artifacts.id`` (preview_artifact_id).
        self.upload_repo = upload_repo
        self.object_store = object_store
        self.source_audio = source_audio
        self.operations: list[str] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        operation = str(call.input.get("operation") or "")
        self.operations.append(operation)
        stored = store_file(
            self.object_store, self.source_audio, purpose=f"fake-{operation}-preview"
        )
        media_info = probe_media(self.source_audio)
        artifact = self.upload_repo.create_artifact(
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
        assert provider.calls[0].idempotency_key == f"{run_id}:{tts_node.id}:tts"


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
        lipsync_node = next(
            node for node in repository.node_runs[run_id] if node.node_id == "LipSync"
        )
        assert provider.artifact_id in lipsync_node.output_artifact_ids


def test_strict_alignment_uses_available_asr_provider(media_fixture_factory):
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        client.app.state.provider_gateway.register(FakeASRProvider())
        _arm_profile(client, "fake.asr", "asr.transcribe", "fake-asr")

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
        alignment_node = next(
            node for node in repository.node_runs[run["id"]] if node.node_id == "NarrationAlignment"
        )
        artifacts = [
            repository.artifacts[artifact_id] for artifact_id in alignment_node.output_artifact_ids
        ]
        narration = next(
            artifact for artifact in artifacts if artifact.kind == ArtifactKind.narration_units
        )
        assert narration.payload["source"] == "asr"
        assert narration.payload["strict"] is True
        assert narration.payload["warnings"] == []


def test_annotation_rerun_degrades_without_source_video():
    """A real vlm.annotation profile but a videoless seed asset must NOT burn a paid
    call: the gated runner degrades to a sensor-only ``vlm_unconfigured`` AnnotationV4
    (never fabricated semantics), the run still completes, and the editor canonical is
    the V4 schema (meta/clips/usage_windows), not a thin labels dict."""
    with TestClient(create_app()) as client:
        _login_admin(client)
        client.app.state.provider_gateway.register(FakeVLMProvider())
        profile = _arm_profile(client, "fake.vlm", "vlm.annotation", "fake-vlm")
        asset_id = "asset_broll_demo"
        # Strip the seeded source artifact in Postgres so the gated runner sees a
        # videoless asset and degrades (vlm_unconfigured) instead of burning a paid call.
        with client.app.state.sqlalchemy_session_factory() as session:
            asset_row = session.get(MediaAssetRow, asset_id)
            asset_row.source_artifact_id = None
            asset_row.annotation_status = "pending"
            asset_row.usable = True
            session.commit()

        rerun = client.post(
            f"/api/annotations/{asset_id}/rerun",
            json={"provider_profile_id": profile.id, "force": True},
        )

        assert rerun.status_code == 202, rerun.text
        assert rerun.json()["status"] == "completed"
        editor = client.get(f"/api/annotations/{asset_id}")
        assert editor.status_code == 200, editor.text
        canonical = editor.json()["canonical"]
        assert canonical["meta"]["annotation_version"] == "annotation_v4"
        assert canonical["clips"] == []
        assert canonical["usage_windows"] == []
        assert canonical["quality_report"]["vlm_status"] == "vlm_unconfigured"
        assert editor.json()["projection"]["vlm_configured"] is False
        # The typed MediaAssetRecord.annotation_status must stay inside its public
        # contract enum (pending/annotated/annotation_failed). The precise
        # "unconfigured" reason lives in quality_report (asserted above) and the
        # projection's vlm_configured flag -- not in this typed field.
        detail = client.get(f"/api/media/assets/{asset_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["asset"]["annotation_status"] == "annotation_failed"
        # A persisted AnnotationV4 artifact must exist for the asset's case.
        with client.app.state.sqlalchemy_session_factory() as session:
            annotations = session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.kind == ArtifactKind.material_annotation.value
                )
            ).all()
        assert annotations


class FakeBgmOmniProvider:
    """Returns canned per-segment semantics for the gated audio.understanding path."""

    provider_id = "fake.bgmomni"

    def invoke(self, call: ProviderCall) -> ProviderResult:
        return ProviderResult(
            output={
                "content": "{}",
                "intent": {
                    "mood": "calm",
                    "role": "climax",
                    "scene_fit": ["产品介绍", "舒缓口播"],
                    "avoid_scene": ["激烈促销"],
                    "reason": "适合舒缓的产品讲解场景",
                },
            }
        )


# Simulate librosa-present objective features (real librosa is an optional dep, absent
# in CI): full-track segments + a beat grid so the audio path has excerpts to listen to.
def _fake_bgm_features(_audio_path):
    return {
        "librosa_available": True,
        "bpm": 120.0,
        "energy": 0.6,
        "tempo_bucket": "mid",
        "loudness_lufs": -14.0,
        "beats": [0.0, 0.5, 10.0, 18.0, 25.0],
        "drops": [18.0],
        "segments": [
            {
                "start": 0.0,
                "end": 30.0,
                "duration": 30.0,
                "energy": 0.8,
                "drop_anchor": 18.0,
                "role_hint": "climax",
            },
        ],
    }


def test_bgm_annotation_rerun_uses_audio_path_and_omni_semantics(monkeypatch):
    """Re-running annotation on a BGM asset takes the audio path: librosa-timed full
    segments (precise seconds + beat grid) enriched by a gated audio.understanding
    (Qwen-Omni) listen per segment -> typed bgm_segments with mood/scene, asset
    annotated + usable. The visual VLM path is never taken."""
    monkeypatch.setattr("packages.media.annotation.bgm.extract_audio_features", _fake_bgm_features)
    # Force a presigned clip URL without real ffmpeg / object store in the test.
    monkeypatch.setattr(
        "apps.api.services.asset_annotation._bgm_audio_urlizer",
        lambda request, path: lambda start, end: "https://fake.local/clip.mp3",
    )
    # Pin a known duration so the window stays in-bounds regardless of the demo seed.
    # The SQLAlchemy BGM annotation path reads the duration from the media repo
    # (asset_source_duration), so pin that source (the in-memory _asset_duration is
    # no longer on this code path).
    monkeypatch.setattr(
        "packages.media.sqlalchemy_repository.SqlAlchemyMediaRepository.asset_source_duration",
        lambda self, asset_id: 30.0,
    )
    with TestClient(create_app()) as client:
        _login_admin(client)
        client.app.state.provider_gateway.register(FakeBgmOmniProvider())
        profile = _arm_profile(client, "fake.bgmomni", "audio.understanding", "qwen3.5-omni-plus")
        asset_id = "asset_bgm_demo"
        assert client.get(f"/api/media/assets/{asset_id}").json()["asset"]["kind"] == "bgm"

        rerun = client.post(
            f"/api/annotations/{asset_id}/rerun",
            json={"provider_profile_id": profile.id, "force": True},
        )

        assert rerun.status_code == 202, rerun.text
        assert rerun.json()["status"] == "completed"
        body = client.get(f"/api/annotations/{asset_id}").json()
        canonical = body["canonical"]
        assert canonical["meta"]["material_type"] == "bgm"
        assert canonical["meta"]["annotation_status"] == "completed"
        segments = canonical["bgm_segments"]
        assert len(segments) == 1
        segment = segments[0]
        assert segment["segment_id"] == "bgm_segment_1"
        assert segment["start"] == 0.0 and segment["end"] == 30.0
        assert segment["drop_anchor_sec"] == 18.0
        assert segment["role"] == "climax"
        assert segment["mood"] == "沉稳"
        assert "产品介绍" in segment["scene_fit"]
        assert segment["source"] == "sensor+audio"
        # beat grid surfaced in quality_report + editor projection
        assert canonical["quality_report"]["bgm"]["beats"] == [0.0, 0.5, 10.0, 18.0, 25.0]
        assert body["projection"]["bgm"]["beats"]
        assert body["projection"]["bgm_segments"]
        assert body["projection"]["usable"] is True
        # asset is annotated + usable so it becomes an eligible BGM candidate
        asset_detail = client.get(f"/api/media/assets/{asset_id}").json()["asset"]
        assert asset_detail["annotation_status"] == "annotated"
        assert asset_detail["usable"] is True
        with client.app.state.sqlalchemy_session_factory() as session:
            annotations = session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.kind == ArtifactKind.material_annotation.value
                )
            ).all()
        assert annotations


def test_bgm_annotation_rerun_degrades_when_features_unavailable(monkeypatch):
    """When objective extraction yields no segments, BGM annotation degrades to
    features-unavailable, marks the asset failed, and never fabricates semantics."""
    monkeypatch.setattr(bgm_annotation, "extract_audio_features", lambda _path: {})

    with TestClient(create_app()) as client:
        _login_admin(client)
        asset_id = "asset_bgm_demo"

        rerun = client.post(f"/api/annotations/{asset_id}/rerun", json={"force": True})

        assert rerun.status_code == 202, rerun.text
        editor = client.get(f"/api/annotations/{asset_id}")
        canonical = editor.json()["canonical"]
        assert canonical["meta"]["material_type"] == "bgm"
        assert canonical["meta"]["annotation_status"] == "failed"
        assert canonical["bgm_segments"] == []
        bgm_report = canonical["quality_report"]["bgm"]
        assert bgm_report["status"] == "features_unavailable"
        assert bgm_report.get("mood") in (None, "")
        assert (
            client.get(f"/api/media/assets/{asset_id}").json()["asset"]["annotation_status"]
            == "annotation_failed"
        )


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


def test_voice_clone_uses_provider_voice_id_and_preview(media_fixture_factory):
    with TestClient(create_app()) as client:
        _login_admin(client)
        provider = FakeVoiceBuildProvider(
            client.app.state.sqlalchemy_upload_repository,
            client.app.state.object_store,
            media_fixture_factory.audio(duration_sec=1.0, filename="voice-clone-preview.wav"),
        )
        client.app.state.provider_gateway.register(provider)
        profile = _arm_profile(client, "fake.voice", "tts.speech", "fake-voice")
        client.app.state.sqlalchemy_upload_repository.create_upload(
            UploadSession(
                id="upl_voice_ref",
                kind=UploadKind.voice_reference,
                filename="voice-ref.wav",
                content_type="audio/wav",
                size_bytes=100,
                status=UploadStatus.completed,
            )
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
