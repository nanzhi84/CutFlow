"""Gated AI cover tests for the ExportFinishedVideo node.

AI cover generation is PAID. These exercise the GATED behaviour:

* With ``cover.mode == "ai"`` AND an enabled real ``image.generate`` profile +
  ACTIVE secret, the node generates the cover through a (mocked, no-network)
  image provider and emits no degradation.
* Without that configuration the node falls back to the EXISTING frame-based
  cover (current behaviour, unchanged) — proving no paid call and no
  fabrication. When AI was *requested* but unavailable, a ``cover_frame_fallback``
  degradation is recorded.

No real keys / money / network: the image provider is a local mock and the
gateway is fully in-memory.
"""

from __future__ import annotations

import json

from packages.ai.gateway import ProviderResult
from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderGateway,
    ProviderRuntimeError,
)
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
    WarningCode,
    WorkflowRun,
)
from packages.core.provider_idempotency import is_provider_call_idempotency_key
from packages.core.storage.object_store import LocalObjectStore, parse_object_uri
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.assets import store_file
from packages.media.cover import DEFAULT_COVER_SIZE, SEEDREAM_COVER_REQUEST_SIZE
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.ai.prompts.registry import PromptRegistry
from packages.production.pipeline.degradation_policies import COVER_FALLBACK_POLICY
from packages.production.pipeline.digital_human import LocalRuntimeAdapter

# A valid 1x1 RGBA PNG — enough for ffprobe to recognise an image stream.
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f"
    "15c4890000000b49444154789c6360000200000500017a5eab3f00000000"
    "49454e44ae426082"
)


def _adapter(tmp_path):
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
    adapter.prompt_registry = PromptRegistry(repository)
    return adapter, gateway, secret_store, object_store


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_cover",
        job_id="job_cover",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_export",
        run_id="run_cover",
        node_id="ExportFinishedVideo",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _seed_final_state(repository, object_store, factory, *, cover_mode: str) -> RunState:
    video_file = factory.video(duration_sec=1.0, filename="final.mp4")
    stored = store_file(object_store, video_file, purpose="final-video")
    final = repository.create_artifact(
        kind=ArtifactKind.video_final,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        run_id="run_cover",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=MediaInfo(media_type="video", codec="h264", format="mp4", duration_sec=1.0),
    )
    timeline = repository.create_artifact(
        kind=ArtifactKind.plan_timeline,
        payload_schema="TimelinePlanArtifact.v1",
        payload={"segments": []},
        case_id="case_demo",
        run_id="run_cover",
    )
    style = repository.create_artifact(
        kind=ArtifactKind.plan_style,
        payload_schema="StylePlanArtifact.v1",
        payload={"font": "default"},
        case_id="case_demo",
        run_id="run_cover",
    )
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        title="封面测试",
        publish_content="漆面修复案例展示",
        script="第一句。第二句。",
        voice={"voice_id": "voice_sandbox"},
        cover={"mode": cover_mode},
    )
    return RunState(
        request=request,
        artifacts={
            ArtifactKind.video_final: final,
            ArtifactKind.plan_timeline: timeline,
            ArtifactKind.plan_style: style,
        },
    )


def _seed_image_profile(repository, secret_ref: str) -> None:
    repository.provider_profiles["openai.image.real"] = ProviderProfile(
        id="openai.image.real",
        provider_id="openai.image",
        model_id="gpt-image-2-all",
        capability="image.generate",
        display_name="OpenAI Image real",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.image.options"),
        default_options={"base_url": "https://example.invalid/v1", "size": "1024x1536"},
    )


def _seed_seedream_image_profile(repository, secret_ref: str) -> None:
    repository.provider_profiles["volcengine.seedream.real"] = ProviderProfile(
        id="volcengine.seedream.real",
        provider_id="volcengine.seedream",
        model_id="doubao-seedream-5-0-260128",
        capability="image.generate",
        display_name="Seedream Image real",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.image.options"),
        default_options={"base_url": "https://ark.cn-beijing.volces.com/api/v3", "size": "2K"},
    )


class _MockImageProvider:
    """Stores a real cover image artifact via the context. No network."""

    provider_id = "openai.image"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.keys: list[str | None] = []

    def invoke_with_context(self, call: ProviderCall, context) -> ProviderResult:
        self.prompts.append(str(call.input.get("prompt") or ""))
        self.keys.append(call.idempotency_key)
        artifact = context.store_media_bytes(
            content=_PNG_1x1,
            filename=f"{call.idempotency_key or 'ai-cover'}.png",
            purpose="covers",
            kind=ArtifactKind.cover_image,
            call=call,
        )
        return ProviderResult(
            output={"cover_artifact_id": artifact.id, "cover_uri": artifact.uri},
            image_count=1,
        )


def test_ai_cover_generates_artifact_when_profile_and_secret_active(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("openai-image-key")
    _seed_image_profile(adapter.repository, secret_ref)
    provider = _MockImageProvider()
    gateway.register(provider)

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    assert cover.uri and cover.uri.startswith("local://")
    assert cover.media_info is not None and cover.media_info.media_type == "image"
    assert cover.payload is not None
    assert cover.payload["source"] == "ai"
    assert cover.payload["provider_id"] == "openai.image"
    assert cover.payload["provider_label"] == "image2"
    assert object_store.exists(parse_object_uri(cover.uri)) is True
    # Generated cover -> no frame-fallback degradation, provider invocation recorded.
    assert output.status == NodeStatus.succeeded
    assert not output.degradations
    assert output.provider_invocation_ids
    # The paid cover call is Run-scoped: stable key, free of node_run.id, so an activity
    # retry recovers the durable invocation instead of generating (and billing) again.
    # Asserted on the ProviderCall: this gateway is transient (no DB-backed reader), and
    # only the durable path copies the key onto the invocation row.
    assert is_provider_call_idempotency_key(provider.keys[0])
    assert ctx.node_run.id not in provider.keys[0]
    # The AI prompt carries the generated cover title (cover prompt build is wired in).
    # No LLM is armed here, so the copy -- and thus the cover headline -- is derived
    # deterministically from the script ("第一句。第二句。" -> cover_title "第一句").
    assert provider.prompts and "第一句" in provider.prompts[0]
    finished = next(
        v for v in adapter.repository.finished_videos.values() if v.run_id == "run_cover"
    )
    assert finished.cover_artifact is not None
    assert finished.cover_artifact.artifact_id == cover.id


class _TemplateAwareImageProvider:
    """Records the cover ProviderCall input so the test can assert the reference
    image + has_template prompt branch reached the provider."""

    provider_id = "openai.image"

    def __init__(self, provider_id: str = "openai.image") -> None:
        self.provider_id = provider_id
        self.inputs: list[dict] = []

    def invoke_with_context(self, call: ProviderCall, context) -> ProviderResult:
        self.inputs.append(dict(call.input))
        artifact = context.store_media_bytes(
            content=_PNG_1x1,
            filename=f"{call.idempotency_key or 'ai-cover'}.png",
            purpose="covers",
            kind=ArtifactKind.cover_image,
            call=call,
        )
        return ProviderResult(
            output={"cover_artifact_id": artifact.id, "cover_uri": artifact.uri},
            image_count=1,
        )


def test_ai_cover_falls_back_from_image2_to_seedream(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    openai_secret = secret_store.put("openai-image-key")
    seedream_secret = secret_store.put("ark-key")
    _seed_image_profile(adapter.repository, openai_secret)
    _seed_seedream_image_profile(adapter.repository, seedream_secret)

    class _FailingOpenAIImageProvider:
        provider_id = "openai.image"

        def invoke_with_context(self, call, context):
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "image2 upstream failed")

    seedream_provider = _TemplateAwareImageProvider(provider_id="volcengine.seedream")
    gateway.register(_FailingOpenAIImageProvider())
    gateway.register(seedream_provider)

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    assert cover.media_info is not None and cover.media_info.media_type == "image"
    assert cover.payload is not None
    assert cover.payload["source"] == "ai"
    assert cover.payload["provider_id"] == "volcengine.seedream"
    assert cover.payload["provider_label"] == "seedream"
    assert cover.payload["fallback_from_provider_profile_ids"] == ["openai.image.real"]
    assert output.status == NodeStatus.succeeded
    assert not output.degradations
    assert len(output.provider_invocation_ids) == 2
    invocations = [
        adapter.repository.provider_invocations[invocation_id]
        for invocation_id in output.provider_invocation_ids
    ]
    assert [item.provider_id for item in invocations] == [
        "openai.image",
        "volcengine.seedream",
    ]
    assert seedream_provider.inputs
    assert seedream_provider.inputs[0]["size"] == SEEDREAM_COVER_REQUEST_SIZE


class _ReferenceDroppingImageProvider:
    provider_id = "openai.image"

    def __init__(self) -> None:
        self.inputs: list[dict] = []

    def invoke_with_context(self, call: ProviderCall, context) -> ProviderResult:
        self.inputs.append(dict(call.input))
        artifact = context.store_media_bytes(
            content=_PNG_1x1,
            filename="provider-dropped-reference.png",
            purpose="covers",
            kind=ArtifactKind.cover_image,
            call=call,
        )
        return ProviderResult(
            output={
                "cover_artifact_id": artifact.id,
                "cover_uri": artifact.uri,
                "reference_image_requested": True,
                "reference_image_used": False,
                "reference_transport": None,
            },
            image_count=1,
        )


def test_ai_cover_consumes_uploaded_cover_template_reference(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("openai-image-key")
    _seed_image_profile(adapter.repository, secret_ref)
    provider = _TemplateAwareImageProvider()
    gateway.register(provider)

    # Seed an uploaded cover_template MediaAsset backed by a real image artifact.
    template_path = tmp_path / "template.png"
    template_path.write_bytes(_PNG_1x1)
    template_stored = store_file(object_store, template_path, purpose="cover-templates")
    template_artifact = adapter.repository.create_artifact(
        kind=ArtifactKind.cover_image,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        uri=template_stored.ref.uri,
        sha256=template_stored.sha256,
        media_info=MediaInfo(media_type="image", codec="png", format="png"),
    )
    from packages.core import contracts as c

    asset = c.MediaAssetRecord(
        id="asset_cover_tpl",
        case_id="case_demo",
        title="风格参考封面",
        kind="cover_template",
        source_artifact_id=template_artifact.id,
    )
    adapter.repository.media_assets[asset.id] = asset

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    state.request.cover.reference_asset_id = asset.id
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert provider.inputs, "image provider must be invoked"
    call_input = provider.inputs[0]
    # The uploaded template and source frame are combined into one edit-reference board.
    import base64

    assert base64.b64decode(call_input.get("reference_image_b64") or "")
    assert call_input.get("reference_filename") == "cover-reference-board.png"
    assert call_input.get("template_image_b64") == call_input.get("reference_image_b64")
    assert call_input.get("source_frame_time_sec") == 0.5
    # ...and the prompt switched to the combined template + source-frame instruction.
    assert "有封面模板参考" in call_input["prompt"]
    assert "参考图是双栏图" in call_input["prompt"]


def test_ai_cover_without_reference_asset_sends_source_frame_reference(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("openai-image-key")
    _seed_image_profile(adapter.repository, secret_ref)
    provider = _TemplateAwareImageProvider()
    gateway.register(provider)

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert provider.inputs
    # No cover template -> the selected video source frame is still passed to image edits.
    assert provider.inputs[0].get("reference_image_b64")
    assert provider.inputs[0].get("reference_filename") == "source-frame.png"
    assert provider.inputs[0].get("source_frame_time_sec") == 0.5
    prompt = provider.inputs[0]["prompt"]
    assert DEFAULT_COVER_SIZE in prompt
    assert "9:16" in prompt
    assert "3:4" not in prompt
    assert "无封面模板参考" in prompt
    assert "本条视频选中的人像/场景帧" in prompt


def test_seedream_ai_cover_request_overrides_size_to_9_16(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("ark-key")
    _seed_seedream_image_profile(adapter.repository, secret_ref)
    provider = _TemplateAwareImageProvider(provider_id="volcengine.seedream")
    gateway.register(provider)

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    assert output.status == NodeStatus.succeeded
    assert provider.inputs
    assert cover.payload is not None
    assert cover.payload["source"] == "ai"
    assert cover.payload["provider_id"] == "volcengine.seedream"
    assert cover.payload["provider_label"] == "seedream"
    assert provider.inputs[0]["size"] == SEEDREAM_COVER_REQUEST_SIZE
    assert SEEDREAM_COVER_REQUEST_SIZE in provider.inputs[0]["prompt"]


def test_ai_cover_prefers_clean_lipsync_source_and_skips_vlm(
    tmp_path, media_fixture_factory, monkeypatch
):
    # The cover source frame must come from the clean lipsync track (no burned
    # subtitles / no b-roll), NOT the final video. Seed a 2.0s lipsync artifact
    # alongside the 1.0s final: the midpoint source frame must reflect the clean
    # source (1.0s), proving the node reads video.lipsync. The deterministic picker
    # also makes NO vlm.annotation provider call.
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("openai-image-key")
    _seed_image_profile(adapter.repository, secret_ref)
    image_provider = _TemplateAwareImageProvider()
    gateway.register(image_provider)

    vlm_called = {"hit": False}

    class _ExplodingVlmProvider:
        provider_id = "dashscope.vlm"

        def invoke(self, call):  # pragma: no cover - must not run
            vlm_called["hit"] = True
            raise AssertionError("deterministic selection must not call the VLM")

    gateway.register(_ExplodingVlmProvider())

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    lipsync_video = media_fixture_factory.video(duration_sec=2.0, filename="lipsync.mp4")
    lipsync_stored = store_file(object_store, lipsync_video, purpose="lipsync-video")
    lipsync = adapter.repository.create_artifact(
        kind=ArtifactKind.video_lipsync,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        run_id="run_cover",
        uri=lipsync_stored.ref.uri,
        sha256=lipsync_stored.sha256,
        media_info=MediaInfo(media_type="video", codec="h264", format="mp4", duration_sec=2.0),
    )
    state.artifacts[ArtifactKind.video_lipsync] = lipsync
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert vlm_called["hit"] is False
    assert image_provider.inputs
    call_input = image_provider.inputs[0]
    assert call_input.get("reference_image_b64")
    # Midpoint of the 2.0s clean lipsync track, not the 1.0s final (would be 0.5).
    assert call_input.get("source_frame_time_sec") == 1.0
    # No vlm.annotation invocation was recorded.
    assert not any(
        invocation.capability_id == "vlm.annotation"
        for invocation in adapter.repository.provider_invocations.values()
    )


def test_ai_cover_continues_to_final_frame_when_clean_sources_are_unreadable(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("openai-image-key")
    _seed_image_profile(adapter.repository, secret_ref)
    provider = _TemplateAwareImageProvider()
    gateway.register(provider)

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    bad_lipsync = adapter.repository.create_artifact(
        kind=ArtifactKind.video_lipsync,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        run_id="run_cover",
        uri="local://cutagent-local/missing/lipsync.mp4",
        media_info=MediaInfo(media_type="video", codec="h264", format="mp4", duration_sec=2.0),
    )
    bad_portrait = adapter.repository.create_artifact(
        kind=ArtifactKind.video_portrait_track,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        run_id="run_cover",
        uri="local://cutagent-local/missing/portrait.mp4",
        media_info=MediaInfo(media_type="video", codec="h264", format="mp4", duration_sec=3.0),
    )
    state.artifacts[ArtifactKind.video_lipsync] = bad_lipsync
    state.artifacts[ArtifactKind.video_portrait_track] = bad_portrait
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert provider.inputs
    call_input = provider.inputs[0]
    assert call_input.get("reference_image_b64")
    assert call_input.get("reference_filename") == "source-frame.png"
    # The unreadable 2s/3s clean artifacts are skipped; final video midpoint is used.
    assert call_input.get("source_frame_time_sec") == 0.5


def test_ai_cover_does_not_accept_provider_output_when_reference_was_dropped(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("openai-image-key")
    _seed_image_profile(adapter.repository, secret_ref)
    provider = _ReferenceDroppingImageProvider()
    gateway.register(provider)

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    assert provider.inputs and provider.inputs[0].get("reference_image_b64")
    assert output.status == NodeStatus.degraded
    assert [d.code for d in output.degradations] == [WarningCode.cover_frame_fallback]
    assert output.degradations[0].policy_id == COVER_FALLBACK_POLICY.id
    assert output.provider_invocation_ids
    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    assert cover.payload is not None
    assert cover.payload["source"] == "frame"
    assert cover.payload["reason"] == "ai_failed"
    assert cover.payload["attempted_provider_profile_ids"] == ["openai.image.real"]
    assert object_store.exists(parse_object_uri(cover.uri or "")) is True


def test_ai_cover_unconfigured_uses_frame_without_degradation(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    # No real image.generate profile is configured -> the deployment has no AI cover
    # capability. With AI as the default mode the frame cover is the honest BASELINE,
    # not a degradation: emitting cover_frame_fallback on every unconfigured run would
    # be noise, so no degradation is recorded. Register a provider that must not fire.
    called = {"hit": False}

    class _ExplodingImageProvider:
        provider_id = "openai.image"

        def invoke_with_context(self, call, context):  # pragma: no cover - must not run
            called["hit"] = True
            raise AssertionError("paid image provider must not be invoked")

    gateway.register(_ExplodingImageProvider())

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    # Frame-based cover: a real image extracted from the final video, no fabrication.
    assert cover.uri and cover.uri.startswith("local://")
    assert cover.media_info is not None and cover.media_info.media_type == "image"
    assert cover.payload is not None
    assert cover.payload["source"] == "frame"
    assert cover.payload["reason"] == "ai_unavailable"
    assert object_store.exists(parse_object_uri(cover.uri)) is True
    # No paid call happened and frame is the baseline -> succeeded, no degradation.
    assert called["hit"] is False
    assert not output.provider_invocation_ids
    assert output.status == NodeStatus.succeeded
    assert not output.degradations


def test_ai_cover_profile_present_but_generation_fails_degrades_to_frame(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    # A real image profile + active secret -> AI cover capability IS available. When
    # the paid call fails for THIS run, falling back to frame IS a real, actionable
    # degradation and must be reported.
    secret_ref = secret_store.put("openai-image-key")
    _seed_image_profile(adapter.repository, secret_ref)

    class _FailingImageProvider:
        provider_id = "openai.image"

        def invoke_with_context(self, call, context):
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "image upstream 500")

    gateway.register(_FailingImageProvider())

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    assert cover.media_info is not None and cover.media_info.media_type == "image"
    assert cover.payload is not None
    assert cover.payload["source"] == "frame"
    assert cover.payload["reason"] == "ai_failed"
    assert cover.payload["attempted_provider_profile_ids"] == ["openai.image.real"]
    assert output.status == NodeStatus.degraded
    assert [d.code for d in output.degradations] == [WarningCode.cover_frame_fallback]
    assert output.degradations[0].policy_id == COVER_FALLBACK_POLICY.id


def _arm_copy_llm(gateway, secret_store, output):
    """Arm the seeded dashscope.llm.prod profile with a fake provider returning a
    publishing-copy JSON, so ExportFinishedVideo generates copy via the LLM path."""
    secret_store.put("dashscope-key", secret_ref="dashscope_prod.secret")

    class _FakeLlmProvider:
        provider_id = "dashscope.llm"

        def invoke(self, call):
            return ProviderResult(
                output={
                    "content": json.dumps(output, ensure_ascii=False),
                    "intent": output,
                }
            )

    gateway.register(_FakeLlmProvider())


def test_finished_title_uses_llm_publish_copy_headline(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    _arm_copy_llm(
        gateway,
        secret_store,
        {
            "title": "轮毂刮花别急着换新",
            "publish_content": "局部修复几百块就能搞定，省下两千多。",
            "cover_title": "轮毂修复省两千",
            "cover_subtitle": "几百块搞定",
        },
    )
    # Frame cover (no image profile) keeps this focused on the title path.
    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="frame"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    finished = next(
        v for v in adapter.repository.finished_videos.values() if v.run_id == "run_cover"
    )
    # The generated headline replaces the request's persona-label title ("封面测试").
    assert finished.title == "轮毂刮花别急着换新"
    assert output.status == NodeStatus.succeeded
    assert len(output.provider_invocation_ids) == 1
    prompt_invocation = next(iter(adapter.repository.prompt_invocations.values()))
    assert prompt_invocation.run_id == "run_cover"
    assert prompt_invocation.node_run_id == "nr_export"
    assert prompt_invocation.provider_invocation_id == output.provider_invocation_ids[0]


def test_ai_cover_prompt_uses_llm_cover_title(tmp_path, media_fixture_factory, monkeypatch):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    _arm_copy_llm(
        gateway,
        secret_store,
        {
            "title": "轮毂刮花别急着换新",
            "publish_content": "局部修复几百块就能搞定。",
            "cover_title": "轮毂修复省两千",
            "cover_subtitle": "几百块搞定",
        },
    )
    secret_ref = secret_store.put("openai-image-key")
    _seed_image_profile(adapter.repository, secret_ref)
    provider = _MockImageProvider()
    gateway.register(provider)

    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="ai"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    assert output.status == NodeStatus.succeeded
    # The AI cover prompt carries the LLM-generated cover_title, not the raw request title.
    assert provider.prompts and "轮毂修复省两千" in provider.prompts[0]
    request_artifact = next(
        artifact for artifact in output.artifacts if artifact.kind == ArtifactKind.provider_raw_request
    )
    payload = request_artifact.payload
    assert payload["cover_title"] == "轮毂修复省两千"
    assert payload["cover_subtitle"] == "几百块搞定"
    assert payload["publish_title"] == "轮毂刮花别急着换新"
    assert payload["request_json"]["prompt"] == provider.prompts[0]
    assert payload["reference"]["provided"] is True
    assert payload["reference"]["image_b64"]["omitted"] is True
    assert "reference_image_b64" in payload["request_json"]
    assert payload["request_json"]["reference_image_b64"]["omitted"] is True


def test_frame_cover_default_mode_has_no_degradation(tmp_path, media_fixture_factory, monkeypatch):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    state = _seed_final_state(
        adapter.repository, object_store, media_fixture_factory, cover_mode="frame"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    assert cover.media_info is not None and cover.media_info.media_type == "image"
    assert cover.payload is not None
    assert cover.payload["source"] == "frame"
    assert cover.payload["reason"] == "requested_frame"
    # Default frame mode is the current behaviour: succeeded, no degradation.
    assert output.status == NodeStatus.succeeded
    assert not output.degradations
    assert not output.provider_invocation_ids


def test_seeded_image_profile_is_gated_without_an_active_secret(tmp_path):
    # The seeded openai.image.prod profile is enabled and its plugin is registered,
    # so the ACTIVE SECRET is the only thing keeping the PAID AI-cover path inert.
    # seed_real_provider_configuration must never ship a secret value (nor drop the
    # profile's secret_ref). This locks that safety property: gated with no secret,
    # armed only once a secret is activated.
    from packages.core.storage.provider_seed import seed_real_provider_configuration

    adapter, gateway, secret_store, _ = _adapter(tmp_path)
    seed_real_provider_configuration(adapter.repository)
    gateway.register(_MockImageProvider())  # provider_id "openai.image" now registered
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        title="封面",
        publish_content="案例",
        script="句。",
        voice={"voice_id": "voice_sandbox"},
        cover={"mode": "ai"},
    )

    # Freshly seeded, no secret activated -> paid AI cover path is NOT armed.
    assert adapter.provider_profiles.image_cover_profile_id(request) is None

    # Activating the seeded profile's secret is the decisive switch that arms it.
    secret_store.put("openai-image-key", secret_ref="openai_image_prod.secret")
    assert adapter.provider_profiles.image_cover_profile_id(request) == "openai.image.prod"


def test_cover_mode_defaults_to_ai():
    # The production request defaults to the AI cover; the frame cover is only the
    # honest fallback when AI is unavailable. A request that does not specify a
    # cover must therefore opt into the AI path.
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        publish_content="案例",
        script="句。",
        voice={"voice_id": "voice_sandbox"},
    )
    assert request.cover.mode == "ai"


def test_export_finished_video_declared_as_provider_side_effect_node():
    # ExportFinishedVideo can make a PAID image.generate call (gated AI cover), so it
    # must be declared as a provider side-effect node WITH an idempotency_key -- like
    # TTS/LipSync -- so reuse/replay accounting is accurate and a rerun cannot silently
    # re-fire the paid cover call unprotected.
    from packages.production.pipeline.digital_human import digital_human_template

    template = digital_human_template()
    node = next(n for n in template.nodes if n.node_id == "ExportFinishedVideo")
    assert node.side_effects == ["provider_call"]
    assert node.idempotency_key is not None
