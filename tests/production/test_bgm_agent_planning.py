from __future__ import annotations

from packages.ai.gateway import ProviderGateway, ProviderResult
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    NodeRun,
    NodeStatus,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    RunStatus,
    WarningCode,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline._materialize import stable_bgm_candidate_id
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
from packages.production.pipeline.nodes import bgm_agent_planning
from packages.production.pipeline.nodes.bgm_agent_planning import (
    _attach_provider_artifacts,
    _compact,
    _parse,
    _unwrap,
)


def _candidate(candidate_id: str = "bgmseg_001") -> dict:
    return {
        "candidate_id": candidate_id,
        "asset_id": "asset_bgm",
        "metadata": {
            "clip_id": "segment_01",
            "mood": "温暖",
            "energy_profile": "steady",
            "scene_fit": ["口播"],
            "script_fit": ["案例介绍"],
            "avoid_script": ["紧急警告"],
        },
    }


class _BgmProvider:
    provider_id = "fake.bgm.llm"

    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.calls = []

    def invoke(self, call):
        self.calls.append(call)
        return ProviderResult(output=self.outputs.pop(0), input_tokens=20, output_tokens=8)


def _material_candidate() -> dict:
    return {
        "asset_id": "asset_bgm",
        "metadata": {
            "clip_id": "segment_01",
            "source_start": 0.0,
            "source_end": 20.0,
            "duration": 20.0,
            "mood": "温暖",
            "energy_profile": "steady",
            "scene_fit": ["口播"],
            "script_fit": ["案例介绍"],
            "avoid_script": ["紧急警告"],
        },
    }


def _ctx(tmp_path, *, outputs: list[dict] | None = None) -> tuple[NodeContext, _BgmProvider | None]:
    repository = Repository()
    object_store = LocalObjectStore(tmp_path / "objects")
    gateway = ProviderGateway(repository, object_store=object_store)
    provider = None
    if outputs is not None:
        repository.provider_profiles["fake.bgm.llm.prod"] = ProviderProfile(
            id="fake.bgm.llm.prod",
            provider_id="fake.bgm.llm",
            model_id="qwen3.7-plus",
            capability="llm.chat",
            display_name="Fake BGM LLM",
            environment="prod",
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.options"),
        )
        provider = _BgmProvider(outputs)
        gateway.register(provider)
    adapter = LocalRuntimeAdapter(
        repository,
        provider_gateway=gateway,
        prompt_registry=PromptRegistry(repository),
        seed_media=False,
    )
    material = repository.create_artifact(
        kind=ArtifactKind.plan_material_pack,
        payload_schema="MaterialPackArtifact.v1",
        payload={"bgm_candidates": [_material_candidate()]},
        case_id="case_demo",
    )
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="温暖介绍案例",
        voice={"voice_id": "voice_sandbox"},
        subtitle={"enabled": False},
        bgm={"enabled": True},
    )
    run = WorkflowRun(
        id="run_bgm_agent",
        job_id="job_bgm_agent",
        case_id="case_demo",
        workflow_template_id="digital_human_editing_agent_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    return (
        NodeContext(
            adapter=adapter,
            run=run,
            node_run=NodeRun(
                id="nr_bgm_agent",
                run_id=run.id,
                node_id="BgmAgentPlanning",
                node_version="v1",
                status=NodeStatus.running,
                input_manifest_hash="sha256:bgm-agent",
            ),
            state=RunState(
                request=request,
                artifacts={ArtifactKind.plan_material_pack: material},
            ),
        ),
        provider,
    )


def test_bgm_response_accepts_only_bgm_id_and_analysis() -> None:
    selected, errors = _parse(
        {"bgm_id": "bgmseg_001", "analysis": "语气匹配"},
        candidates=[_candidate()],
    )

    assert selected == "bgmseg_001"
    assert errors == []


def test_bgm_response_rejects_caption_or_geometry_overreach() -> None:
    selected, errors = _parse(
        {
            "bgm_id": "bgmseg_001",
            "analysis": "语气匹配",
            "caption_choices": [],
        },
        candidates=[_candidate()],
    )

    assert selected == "bgmseg_001"
    assert errors == ["BGM response must contain exactly bgm_id and analysis"]


def test_bgm_response_allows_explicit_null_and_rejects_unknown_id() -> None:
    assert _parse({"bgm_id": None, "analysis": "本条不宜配乐"}, candidates=[_candidate()]) == (
        None,
        [],
    )

    selected, errors = _parse({"bgm_id": "invented", "analysis": ""}, candidates=[_candidate()])
    assert selected == "invented"
    assert errors == ["bgm_id 'invented' is not a known candidate"]


def test_provider_envelope_is_exact_and_unwraps_intent() -> None:
    payload = {"bgm_id": "bgmseg_001", "analysis": "fit"}

    assert _unwrap(payload) == (payload, [])
    assert _unwrap({"content": "ok", "intent": payload}) == (payload, [])
    unwrapped, errors = _unwrap({"content": "ok", "intent": payload, "extra": 1})
    assert unwrapped == payload
    assert errors == ["BGM provider envelope must contain exactly content and intent"]


def test_provider_response_type_errors_are_explicit() -> None:
    assert _unwrap("not-json") == ({}, ["BGM provider output must be a JSON object"])
    assert _unwrap({"content": 1, "intent": []}) == (
        {},
        [
            "BGM provider envelope content must be a string",
            "BGM provider envelope intent must be an object",
        ],
    )
    selected, errors = _parse(
        {"bgm_id": 7, "analysis": 9},
        candidates=[_candidate()],
    )
    assert selected is None
    assert "BGM response analysis must be a string" in errors
    assert "BGM response bgm_id must be null or a string" in errors


def test_attaching_provider_evidence_ignores_missing_invocation(tmp_path) -> None:
    ctx, _ = _ctx(tmp_path)
    artifact = ctx.artifact(ArtifactKind.provider_raw_request, {}, "fixture")
    _attach_provider_artifacts(
        ctx=ctx,
        invocation_id="missing",
        request_artifact=artifact,
        response_artifact=artifact,
    )


def test_compact_prompt_candidate_contains_no_caption_fields() -> None:
    compact = _compact(_candidate())

    assert compact == {
        "bgm_id": "bgmseg_001",
        "asset_id": "asset_bgm",
        "segment_id": "segment_01",
        "mood": "温暖",
        "energy_profile": "steady",
        "scene_fit": ["口播"],
        "script_fit": ["案例介绍"],
        "avoid_script": ["紧急警告"],
    }
    assert all("caption" not in key for key in compact)


def test_bgm_agent_success_persists_provider_evidence_and_selected_segment(tmp_path) -> None:
    candidate_id = stable_bgm_candidate_id(_material_candidate())
    ctx, provider = _ctx(
        tmp_path,
        outputs=[{"bgm_id": candidate_id, "analysis": "温暖口播匹配"}],
    )

    output = bgm_agent_planning.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert output.warnings == []
    assert len(output.provider_invocation_ids) == 1
    assert provider is not None and len(provider.calls) == 1
    style = next(item for item in output.artifacts if item.kind == ArtifactKind.plan_style)
    diagnostics = next(
        item for item in output.artifacts if item.kind == ArtifactKind.plan_bgm_diagnostics
    )
    assert style.payload["bgm"]["asset_id"] == "asset_bgm"
    assert style.payload["bgm"]["segment_id"] == "segment_01"
    assert diagnostics.payload["planned"] is True
    assert diagnostics.payload["reason"] == "selected"
    invocation = ctx.repository.provider_invocations[output.provider_invocation_ids[0]]
    assert invocation.request_artifact_id
    assert invocation.response_artifact_id


def test_bgm_agent_does_not_report_default_caption_font_as_bgm_degradation(tmp_path) -> None:
    candidate_id = stable_bgm_candidate_id(_material_candidate())
    ctx, _ = _ctx(
        tmp_path,
        outputs=[{"bgm_id": candidate_id, "analysis": "温暖口播匹配"}],
    )
    ctx.state.request = ctx.state.request.model_copy(
        update={
            "subtitle": ctx.state.request.subtitle.model_copy(
                update={"enabled": True, "normal_enabled": True, "font_id": None}
            )
        }
    )

    output = bgm_agent_planning.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert output.warnings == []
    assert output.degradations == []
    style = next(item for item in output.artifacts if item.kind == ArtifactKind.plan_style)
    assert style.payload["font"]["font_id"] == "case_default_font"


def test_bgm_agent_repairs_unknown_id_then_succeeds(tmp_path) -> None:
    candidate_id = stable_bgm_candidate_id(_material_candidate())
    ctx, provider = _ctx(
        tmp_path,
        outputs=[
            {"bgm_id": "invented", "analysis": "bad"},
            {"bgm_id": candidate_id, "analysis": "repaired"},
        ],
    )

    output = bgm_agent_planning.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert provider is not None and len(provider.calls) == 2
    diagnostics = next(
        item.payload for item in output.artifacts if item.kind == ArtifactKind.plan_bgm_diagnostics
    )
    assert [item["error_count"] for item in diagnostics["repair_trace"]] == [1, 0]
    assert len(output.provider_invocation_ids) == 2


def test_bgm_agent_without_provider_falls_back_visibly(tmp_path) -> None:
    ctx, provider = _ctx(tmp_path)

    output = bgm_agent_planning.run(ctx)

    assert provider is None
    assert output.status == NodeStatus.degraded
    assert WarningCode.bgm_planning_failed in output.warnings
    diagnostics = next(
        item.payload for item in output.artifacts if item.kind == ArtifactKind.plan_bgm_diagnostics
    )
    assert diagnostics["planned"] is False
    assert diagnostics["reason"] == "no_provider"
    assert diagnostics["bgm_id"] == stable_bgm_candidate_id(_material_candidate())


def test_bgm_agent_with_empty_library_degrades_explicitly(tmp_path) -> None:
    ctx, _ = _ctx(tmp_path)
    material = ctx.state.require(ArtifactKind.plan_material_pack).model_copy(
        update={"payload": {"bgm_candidates": []}}
    )
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = material

    output = bgm_agent_planning.run(ctx)

    assert output.status == NodeStatus.degraded
    assert WarningCode.bgm_skipped_library_unannotated in output.warnings
    diagnostics = next(
        item.payload for item in output.artifacts if item.kind == ArtifactKind.plan_bgm_diagnostics
    )
    assert diagnostics["reason"] == "no_candidates"


def test_bgm_agent_unrepairable_response_uses_deterministic_fallback(tmp_path) -> None:
    ctx, provider = _ctx(
        tmp_path,
        outputs=[
            {"bgm_id": "invented", "analysis": 123},
            {"bgm_id": "still-invented", "analysis": 456},
        ],
    )

    output = bgm_agent_planning.run(ctx)

    assert provider is not None and len(provider.calls) == 2
    assert output.status == NodeStatus.degraded
    diagnostics = next(
        item.payload for item in output.artifacts if item.kind == ArtifactKind.plan_bgm_diagnostics
    )
    assert diagnostics["reason"] == "unrepairable"
    assert diagnostics["planned"] is False


def test_bgm_agent_non_recovery_provider_error_uses_visible_fallback(tmp_path, monkeypatch) -> None:
    ctx, _ = _ctx(tmp_path, outputs=[{"bgm_id": None, "analysis": "unused"}])

    def fail_invoke(**_kwargs):
        raise NodeExecutionError(ErrorCode.provider_remote_failed, "provider unavailable")

    monkeypatch.setattr(bgm_agent_planning, "_invoke", fail_invoke)

    output = bgm_agent_planning.run(ctx)

    assert output.status == NodeStatus.degraded
    diagnostics = next(
        item.payload for item in output.artifacts if item.kind == ArtifactKind.plan_bgm_diagnostics
    )
    assert diagnostics["reason"] == "provider_error"
