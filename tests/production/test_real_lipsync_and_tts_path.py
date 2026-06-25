"""Real-path (mocked-provider) tests for TTS subtitle source + lipsync fallback.

These exercise the GATED real path. They register mock provider plugins and seed
real ProviderProfiles with ACTIVE secrets so the pipeline takes the real branch.
The existing 302 tests run with NO secrets and keep the sandbox path — see
``test_real_lipsync_falls_back_to_sandbox_without_secret`` for the no-secret
byte-identical guarantee.
"""

from __future__ import annotations

import pytest

from packages.ai.gateway import ProviderResult
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway, ProviderRuntimeError
from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    MediaInfo,
    NodeRun,
    NodeStatus,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore
from packages.core.workflow import NodeExecutionError
from packages.media.assets import store_file
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


def _real_lipsync_profile(provider_id: str, profile_id: str, secret_ref: str) -> ProviderProfile:
    return ProviderProfile(
        id=profile_id,
        provider_id=provider_id,
        model_id="real-model",
        capability="lipsync.video",
        display_name=profile_id,
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.lipsync.options"),
    )


def _adapter(tmp_path) -> tuple[LocalRuntimeAdapter, ProviderGateway, LocalSecretStore, LocalObjectStore]:
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    object_store = LocalObjectStore(tmp_path / "objects")
    gateway = ProviderGateway(
        repository,
        secret_store=secret_store,
        object_store=object_store,
        auto_register_real_plugins=False,
    )
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    adapter.provider_gateway = gateway
    return adapter, gateway, secret_store, object_store


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_1",
        job_id="job_1",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_lipsync",
        run_id="run_1",
        node_id="LipSync",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _media_info(kind: str) -> MediaInfo:
    return MediaInfo(media_type=kind, codec="h264" if kind == "video" else "mp3", format="mp4", duration_sec=2.0)


def _lipsync_state(
    repository,
    object_store,
    factory,
    *,
    profile_id: str,
    timeout_minutes: int = 30,
) -> RunState:
    portrait_file = factory.video(duration_sec=2.0, filename="portrait.mp4")
    audio_file = factory.audio(duration_sec=2.0, filename="speech.wav")
    portrait_stored = store_file(object_store, portrait_file, purpose="portrait")
    audio_stored = store_file(object_store, audio_file, purpose="audio")
    portrait = repository.create_artifact(
        kind=ArtifactKind.video_portrait_track,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        uri=portrait_stored.ref.uri,
        sha256=portrait_stored.sha256,
        media_info=_media_info("video"),
    )
    audio = repository.create_artifact(
        kind=ArtifactKind.audio_tts,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        uri=audio_stored.ref.uri,
        sha256=audio_stored.sha256,
        media_info=_media_info("audio"),
    )
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="第一句。第二句。",
        voice={"voice_id": "voice_sandbox"},
        lipsync={
            "enabled": True,
            "provider_profile_id": profile_id,
            "timeout_minutes": timeout_minutes,
        },
    )
    return RunState(
        request=request,
        artifacts={ArtifactKind.video_portrait_track: portrait, ArtifactKind.audio_tts: audio},
    )


class _StoringLipSyncProvider:
    """Mock provider that stores a real video artifact via the context."""

    def __init__(self, provider_id: str, video_file) -> None:
        self.provider_id = provider_id
        self.video_file = video_file
        self.calls: list[str] = []

    def invoke_with_context(self, call: ProviderCall, context) -> ProviderResult:
        self.calls.append(call.idempotency_key or "")
        artifact = context.store_media_bytes(
            content=self.video_file.read_bytes(),
            filename=f"{self.provider_id}.mp4",
            purpose="generated-video",
            kind=ArtifactKind.video_lipsync,
            call=call,
            tier="ephemeral",
        )
        return ProviderResult(
            output={"video_artifact_id": artifact.id, "video_uri": artifact.uri, "report": "pass"},
            video_seconds=float(call.input.get("duration_sec") or 0),
        )


class _CapturingLipSyncProvider(_StoringLipSyncProvider):
    def __init__(self, provider_id: str, video_file) -> None:
        super().__init__(provider_id, video_file)
        self.inputs: list[dict] = []

    def invoke_with_context(self, call: ProviderCall, context) -> ProviderResult:
        self.inputs.append(call.input)
        return super().invoke_with_context(call, context)


class _FailingLipSyncProvider:
    def __init__(self, provider_id: str, code: ErrorCode, message: str) -> None:
        self.provider_id = provider_id
        self.code = code
        self.message = message
        self.calls = 0

    def invoke_with_context(self, call: ProviderCall, context) -> ProviderResult:
        self.calls += 1
        raise ProviderRuntimeError(self.code, self.message)


class _SubtitleTTSProvider:
    """Mock real TTS provider that returns a stored audio artifact + subtitle segments."""

    provider_id = "minimax.tts"

    def __init__(self, audio_file, segments) -> None:
        self.audio_file = audio_file
        self.segments = segments

    def invoke_with_context(self, call: ProviderCall, context) -> ProviderResult:
        assert call.input.get("subtitle") is True  # real path requests subtitles
        artifact = context.store_media_bytes(
            content=self.audio_file.read_bytes(),
            filename="minimax-tts.mp3",
            purpose="generated-audio",
            kind=ArtifactKind.audio_tts,
            call=call,
        )
        return ProviderResult(
            output={
                "audio_artifact_id": artifact.id,
                "audio_uri": artifact.uri,
                "subtitle_segments": self.segments,
            },
            audio_seconds=2.0,
        )


def test_real_tts_subtitle_becomes_primary_narration_source(tmp_path, media_fixture_factory, monkeypatch):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("minimax-key")
    segments = [
        {"start": 0.0, "end": 1.0, "text": "第一句。"},
        {"start": 1.0, "end": 2.0, "text": "第二句。"},
    ]
    gateway.register(
        _SubtitleTTSProvider(media_fixture_factory.audio(duration_sec=2.0, filename="tts.wav"), segments)
    )
    adapter.repository.provider_profiles["minimax.real"] = ProviderProfile(
        id="minimax.real",
        provider_id="minimax.tts",
        model_id="speech-02-hd",
        capability="tts.speech",
        display_name="MiniMax real",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
        default_options={"group_id": "group-1"},
    )
    adapter.repository.voices["voice_real"] = adapter.repository.voices["voice_sandbox"].model_copy(
        update={"id": "voice_real", "provider_profile_id": "minimax.real"}
    )

    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="第一句。第二句。",
        voice={"voice_id": "voice_real", "provider_profile_id": "minimax.real"},
        strictness={"strict_timestamps": True},
    )
    state = RunState(request=request)

    from packages.production.pipeline import nodes

    tts_run = NodeRun(
        id="nr_tts", run_id="run_1", node_id="TTS", node_version="v1",
        status=NodeStatus.running, input_manifest_hash="sha256:test",
    )
    tts_ctx = NodeContext(adapter=adapter, run=_run(), node_run=tts_run, state=state)
    tts_output = nodes.tts.run(tts_ctx)
    audio_artifact = tts_output.artifacts[0]
    assert audio_artifact.uri and audio_artifact.uri.startswith("local://")
    assert audio_artifact.media_info and audio_artifact.media_info.media_type == "audio"
    # subtitle segments stashed for narration alignment
    assert state.scratch["tts_subtitle_segments"] == segments
    state.artifacts[ArtifactKind.audio_tts] = audio_artifact

    align_run = NodeRun(
        id="nr_align", run_id="run_1", node_id="NarrationAlignment", node_version="v1",
        status=NodeStatus.running, input_manifest_hash="sha256:test",
    )
    align_ctx = NodeContext(adapter=adapter, run=_run(), node_run=align_run, state=state)
    align_output = nodes.narration_alignment.run(align_ctx)
    narration = next(a for a in align_output.artifacts if a.kind == ArtifactKind.narration_units).payload
    assert narration["source"] == "tts_subtitle"
    assert narration["strict"] is True
    assert [u["text"] for u in narration["units"]] == ["第一句。", "第二句。"]
    assert narration["units"][0]["start"] == 0.0
    assert narration["units"][1]["end"] == 2.0


def test_real_heygem_failure_does_not_fall_back_to_videoretalk(tmp_path, media_fixture_factory, monkeypatch):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("rh-key")
    ds_secret = secret_store.put("ds-key")
    gateway.register(_FailingLipSyncProvider("runninghub.heygem", ErrorCode.provider_remote_failed, "boom"))
    videoretalk = _StoringLipSyncProvider(
        "dashscope.videoretalk",
        media_fixture_factory.video(duration_sec=2.0, filename="vrt.mp4"),
    )
    gateway.register(videoretalk)
    adapter.repository.provider_profiles["heygem.real"] = _real_lipsync_profile(
        "runninghub.heygem", "heygem.real", secret_ref
    )
    adapter.repository.provider_profiles["videoretalk.real"] = _real_lipsync_profile(
        "dashscope.videoretalk", "videoretalk.real", ds_secret
    )

    state = _lipsync_state(adapter.repository, object_store, media_fixture_factory, profile_id="heygem.real")
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    from packages.production.pipeline import nodes

    with pytest.raises(NodeExecutionError) as exc:
        nodes.lipsync.run(ctx)

    assert exc.value.error.code == ErrorCode.provider_remote_failed
    assert exc.value.error.message == "boom"
    assert videoretalk.calls == []


def test_real_lipsync_call_carries_request_timeout_minutes(tmp_path, media_fixture_factory, monkeypatch):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("rh-key")
    heygem = _CapturingLipSyncProvider(
        "runninghub.heygem", media_fixture_factory.video(duration_sec=2.0, filename="heygem.mp4")
    )
    gateway.register(heygem)
    adapter.repository.provider_profiles["heygem.real"] = _real_lipsync_profile(
        "runninghub.heygem", "heygem.real", secret_ref
    )

    state = _lipsync_state(
        adapter.repository,
        object_store,
        media_fixture_factory,
        profile_id="heygem.real",
        timeout_minutes=45,
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    from packages.production.pipeline import nodes

    nodes.lipsync.run(ctx)

    assert heygem.inputs[0]["timeout_minutes"] == 45


def test_real_videoretalk_content_policy_failure_does_not_fall_back_to_heygem(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    ds_secret = secret_store.put("ds-key")
    rh_secret = secret_store.put("rh-key")
    gateway.register(
        _FailingLipSyncProvider(
            "dashscope.videoretalk",
            ErrorCode.provider_remote_failed,
            "Input data may contain inappropriate content.",
        )
    )
    heygem = _StoringLipSyncProvider(
        "runninghub.heygem", media_fixture_factory.video(duration_sec=2.0, filename="hg.mp4")
    )
    gateway.register(heygem)
    adapter.repository.provider_profiles["videoretalk.real"] = _real_lipsync_profile(
        "dashscope.videoretalk", "videoretalk.real", ds_secret
    )
    adapter.repository.provider_profiles["heygem.real"] = _real_lipsync_profile(
        "runninghub.heygem", "heygem.real", rh_secret
    )

    state = _lipsync_state(adapter.repository, object_store, media_fixture_factory, profile_id="videoretalk.real")
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    from packages.production.pipeline import nodes

    with pytest.raises(NodeExecutionError) as exc:
        nodes.lipsync.run(ctx)

    assert exc.value.error.code == ErrorCode.provider_remote_failed
    assert "inappropriate content" in exc.value.error.message.lower()
    assert heygem.calls == []


def test_real_videoretalk_non_policy_failure_does_not_fall_back(tmp_path, media_fixture_factory, monkeypatch):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    ds_secret = secret_store.put("ds-key")
    rh_secret = secret_store.put("rh-key")
    gateway.register(
        _FailingLipSyncProvider("dashscope.videoretalk", ErrorCode.provider_remote_failed, "transient network error")
    )
    heygem = _StoringLipSyncProvider(
        "runninghub.heygem", media_fixture_factory.video(duration_sec=2.0, filename="hg.mp4")
    )
    gateway.register(heygem)
    adapter.repository.provider_profiles["videoretalk.real"] = _real_lipsync_profile(
        "dashscope.videoretalk", "videoretalk.real", ds_secret
    )
    adapter.repository.provider_profiles["heygem.real"] = _real_lipsync_profile(
        "runninghub.heygem", "heygem.real", rh_secret
    )

    state = _lipsync_state(adapter.repository, object_store, media_fixture_factory, profile_id="videoretalk.real")
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    from packages.production.pipeline import nodes

    with pytest.raises(NodeExecutionError) as exc:
        nodes.lipsync.run(ctx)
    assert exc.value.error.code == ErrorCode.provider_remote_failed
    # VideoReTalk -> HeyGem only happens on content-policy; otherwise no fallback call
    assert heygem.calls == []


def test_real_lipsync_falls_back_to_sandbox_without_secret(tmp_path, media_fixture_factory, monkeypatch):
    """No active secret -> the real path is NOT taken; sandbox pass-through runs."""
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    # profile points at a real provider but has a secret_ref with NO stored secret
    gateway.register(_StoringLipSyncProvider("runninghub.heygem", media_fixture_factory.video(duration_sec=2.0)))
    adapter.repository.provider_profiles["heygem.real"] = _real_lipsync_profile(
        "runninghub.heygem", "heygem.real", "runninghub_prod.secret"
    )
    # seed a sandbox lipsync profile to route the no-secret path
    adapter.repository.provider_profiles["runninghub.heygem.default"] = ProviderProfile(
        id="runninghub.heygem.default",
        provider_id="sandbox",
        model_id="heygem.local",
        capability="lipsync.video",
        display_name="Sandbox HeyGem",
        environment="local",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.lipsync.options"),
    )
    from packages.ai.gateway.provider_gateway import SandboxProvider

    gateway.register(SandboxProvider())

    state = _lipsync_state(
        adapter.repository, object_store, media_fixture_factory, profile_id="heygem.real"
    )
    profile, is_real = adapter.provider_profiles.resolve_lipsync(state.request)
    assert is_real is False  # gating: no secret -> not real
    # the node will route the requested profile id to the gateway; point it at sandbox
    state.request.lipsync.provider_profile_id = "runninghub.heygem.default"
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    from packages.production.pipeline import nodes

    output = nodes.lipsync.run(ctx)
    report = next(a for a in output.artifacts if a.kind == ArtifactKind.lipsync_report).payload
    video = next(a for a in output.artifacts if a.kind == ArtifactKind.video_lipsync)
    # byte-identical sandbox behavior: pass-through portrait, skipped report
    assert report["skipped"] is True
    assert report["skipped_reason"] == "sandbox.pass_through"
    assert "sandbox_lipsync_passthrough" in report["warnings"]
    assert video.uri == state.artifacts[ArtifactKind.video_portrait_track].uri
