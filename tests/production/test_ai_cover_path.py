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

from packages.ai.gateway import ProviderResult
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway
from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    MediaInfo,
    NodeRun,
    NodeStatus,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    RunStatus,
    WarningCode,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore, parse_object_uri
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.assets import store_file
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
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


class _MockImageProvider:
    """Stores a real cover image artifact via the context. No network."""

    provider_id = "openai.image"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke_with_context(self, call: ProviderCall, context) -> ProviderResult:
        self.prompts.append(str(call.input.get("prompt") or ""))
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

    state = _seed_final_state(adapter.repository, object_store, media_fixture_factory, cover_mode="ai")
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    assert cover.uri and cover.uri.startswith("local://")
    assert cover.media_info is not None and cover.media_info.media_type == "image"
    assert object_store.exists(parse_object_uri(cover.uri)) is True
    # Generated cover -> no frame-fallback degradation, provider invocation recorded.
    assert output.status == NodeStatus.succeeded
    assert not output.degradations
    assert output.provider_invocation_ids
    # The AI prompt carried the real title (cover prompt build is wired in).
    assert provider.prompts and "封面测试" in provider.prompts[0]
    finished = next(v for v in adapter.repository.finished_videos.values() if v.run_id == "run_cover")
    assert finished.cover_artifact is not None
    assert finished.cover_artifact.artifact_id == cover.id


class _TemplateAwareImageProvider:
    """Records the cover ProviderCall input so the test can assert the reference
    image + has_template prompt branch reached the provider."""

    provider_id = "openai.image"

    def __init__(self) -> None:
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

    state = _seed_final_state(adapter.repository, object_store, media_fixture_factory, cover_mode="ai")
    state.request.cover.reference_asset_id = asset.id
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert provider.inputs, "image provider must be invoked"
    call_input = provider.inputs[0]
    # The uploaded template bytes were forwarded to the edit-reference path...
    import base64

    assert call_input.get("template_image_b64") == base64.b64encode(_PNG_1x1).decode("ascii")
    assert call_input.get("template_filename")
    # ...and the prompt switched to the has_template style-reference instruction.
    assert "style and layout template" in call_input["prompt"]


def test_ai_cover_without_reference_asset_sends_no_template(
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

    state = _seed_final_state(adapter.repository, object_store, media_fixture_factory, cover_mode="ai")
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert provider.inputs
    # No reference asset -> no template bytes, text-to-image generation only.
    assert "template_image_b64" not in provider.inputs[0]
    assert "from scratch" in provider.inputs[0]["prompt"]


def test_ai_cover_requested_but_unconfigured_falls_back_to_frame_cover(
    tmp_path, media_fixture_factory, monkeypatch
):
    adapter, gateway, secret_store, object_store = _adapter(tmp_path)
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    # No image provider registered, no profile, no secret -> the PAID path is
    # unreachable. Register a provider that would explode if ever called.
    called = {"hit": False}

    class _ExplodingImageProvider:
        provider_id = "openai.image"

        def invoke_with_context(self, call, context):  # pragma: no cover - must not run
            called["hit"] = True
            raise AssertionError("paid image provider must not be invoked")

    gateway.register(_ExplodingImageProvider())

    state = _seed_final_state(adapter.repository, object_store, media_fixture_factory, cover_mode="ai")
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    from packages.production.pipeline import nodes

    output = nodes.export_finished_video.run(ctx)

    cover = next(a for a in output.artifacts if a.kind == ArtifactKind.cover_image)
    # Frame-based cover: a real image extracted from the final video, no fabrication.
    assert cover.uri and cover.uri.startswith("local://")
    assert cover.media_info is not None and cover.media_info.media_type == "image"
    assert object_store.exists(parse_object_uri(cover.uri)) is True
    # No paid call happened; degraded with the honest frame-fallback notice.
    assert called["hit"] is False
    assert not output.provider_invocation_ids
    assert output.status == NodeStatus.degraded
    assert [d.code for d in output.degradations] == [WarningCode.cover_frame_fallback]
    assert output.degradations[0].policy_id == COVER_FALLBACK_POLICY.id


def test_frame_cover_default_mode_has_no_degradation(
    tmp_path, media_fixture_factory, monkeypatch
):
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
    assert adapter._image_cover_profile_id(request) is None

    # Activating the seeded profile's secret is the decisive switch that arms it.
    secret_store.put("openai-image-key", secret_ref="openai_image_prod.secret")
    assert adapter._image_cover_profile_id(request) == "openai.image.prod"


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
