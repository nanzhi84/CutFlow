"""Legacy ``digital_human_editing_agent_v1`` compatibility wrapper.

The active v2 workflow uses ``MediaSelectionAgentPlanning`` and separate
post-processing nodes. This wrapper remains only so historical v1 runs can resume
with their original combined media/BGM/huazi semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.core.contracts import (
    ArtifactKind,
    DegradationNotice,
    NodeStatus,
    WarningCode,
)
from packages.core.workflow import NodeOutput
from packages.production.pipeline._materialize import materialize_style_from_selection
from packages.production.pipeline._legacy_editing_agent_planning import (
    EditingAgentContext,
    EditingAgentSelectionResult,
    _compact_prompt_input as _media_compact_prompt_input,
    _repair_broll_selection_to_constraints as _media_repair_broll_selection,
    build_editing_agent_context as _build_media_selection_context,
    materialize_media_selection_outputs,
    select_editing_assignment,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._creative_intent import load_creative_intent
from packages.production.pipeline.nodes.legacy_huazi_planning import (
    HuaziPlanningResult,
    _empty_result,
    plan_huazi_overlays,
)


def _compact_prompt_input(agent_input: dict, *, include_bgm: bool = True) -> dict:
    return _media_compact_prompt_input(agent_input, include_bgm=include_bgm)


def _repair_broll_selection_to_constraints(**kwargs):
    return _media_repair_broll_selection(**kwargs)


def _empty_huazi_result(reason: str) -> HuaziPlanningResult:
    return _empty_result(reason)


@dataclass(frozen=True)
class EditingAgentMaterializedOutputs:
    assignment_payload: dict
    portrait_payload: dict
    broll_payload: dict
    style_payload: dict
    diagnostics: dict
    warnings: list[WarningCode]
    degradations: list[DegradationNotice]


def build_editing_agent_context(
    *,
    request,
    material: dict,
    narration: dict,
    boundary: dict,
    windows: dict,
    creative_intent=None,
    retrieval: dict | None = None,
) -> EditingAgentContext:
    """Preserve the v1/test call surface; creative intent is owned by Huazi now."""

    del creative_intent
    return _build_media_selection_context(
        request=request,
        material=material,
        narration=narration,
        boundary=boundary,
        windows=windows,
        retrieval=retrieval,
        include_bgm=True,
    )


def materialize_editing_outputs(
    *,
    request,
    node_id: str,
    agent_context: EditingAgentContext,
    selection_result: EditingAgentSelectionResult,
    huazi_result: HuaziPlanningResult,
) -> EditingAgentMaterializedOutputs:
    selection = selection_result.selection
    media = materialize_media_selection_outputs(
        request=request,
        node_id=node_id,
        agent_context=agent_context,
        selection_result=selection_result,
        assignment_bgm_id=selection.bgm_id,
    )
    warnings = list(media.warnings)
    degradations = list(media.degradations)
    warnings.extend(huazi_result.warnings)
    degradations.extend(
        notice.model_copy(update={"node_id": node_id}) for notice in huazi_result.degradations
    )
    style_payload, style_warnings, style_degradations = materialize_style_from_selection(
        request=request,
        material=agent_context.shortlisted_material,
        overlay_events=huazi_result.overlay_events,
        bgm_id=selection.bgm_id,
    )
    warnings.extend(style_warnings)
    degradations.extend(
        notice.model_copy(update={"node_id": node_id}) for notice in style_degradations
    )
    candidates = agent_context.candidates
    diagnostics = {
        **media.diagnostics,
        "font_id": None,
        "request_font_id": request.subtitle.font_id,
        "huazi_choices": huazi_result.diagnostics.get("choices", []),
        "huazi_diagnostics": {
            key: value for key, value in huazi_result.diagnostics.items() if key != "choices"
        },
        "bgm_id": selection.bgm_id,
        "candidate_counts": {
            **media.diagnostics["candidate_counts"],
            "font": len(candidates.font_by_id),
            "bgm": len(candidates.bgm_by_id),
        },
    }
    return EditingAgentMaterializedOutputs(
        assignment_payload=media.assignment_payload,
        portrait_payload=media.portrait_payload,
        broll_payload=media.broll_payload,
        style_payload=style_payload,
        diagnostics=diagnostics,
        warnings=warnings,
        degradations=degradations,
    )


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    boundary = state.require(ArtifactKind.plan_narration_boundary).payload or {}
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}
    retrieval_artifact = state.artifacts.get(ArtifactKind.plan_window_material_retrieval)
    retrieval = retrieval_artifact.payload if retrieval_artifact is not None else None
    creative_intent = load_creative_intent(state)

    agent_context = build_editing_agent_context(
        request=state.request,
        material=material,
        narration=narration,
        boundary=boundary,
        windows=windows,
        creative_intent=creative_intent,
        retrieval=retrieval,
    )
    selection_result = select_editing_assignment(ctx=ctx, agent_context=agent_context)
    huazi_result = plan_huazi_overlays(
        ctx=ctx,
        agent_context=agent_context,
        selection_result=selection_result,
        creative_intent=creative_intent,
    )
    materialized = materialize_editing_outputs(
        request=state.request,
        node_id=ctx.node_run.node_id,
        agent_context=agent_context,
        selection_result=selection_result,
        huazi_result=huazi_result,
    )

    return NodeOutput(
        status=NodeStatus.degraded if materialized.degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_media_assignment,
                materialized.assignment_payload,
                "MediaAssignmentPlan.v1",
            ),
            ctx.artifact(
                ArtifactKind.plan_portrait,
                materialized.portrait_payload,
                "PortraitPlanArtifact.v1",
            ),
            ctx.artifact(
                ArtifactKind.plan_broll,
                materialized.broll_payload,
                "BrollPlanArtifact.v1",
            ),
            ctx.artifact(
                ArtifactKind.plan_style, materialized.style_payload, "StylePlanArtifact.v1"
            ),
            ctx.artifact(
                ArtifactKind.plan_editing_diagnostics,
                materialized.diagnostics,
                "EditingAgentDiagnostics.v1",
            ),
        ],
        warnings=materialized.warnings,
        degradations=materialized.degradations,
        provider_invocation_ids=[
            *selection_result.provider_invocation_ids,
            *huazi_result.provider_invocation_ids,
        ],
    )
