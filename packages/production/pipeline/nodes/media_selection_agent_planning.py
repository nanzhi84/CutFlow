"""MediaSelectionAgentPlanning: choose and materialize portrait/B-roll only.

The active editing-agent v2 workflow keeps media selection isolated from final
caption/BGM post-processing. This node therefore never reads CreativeIntent,
never invokes the legacy HuaziPlanningSubagent, and never emits ``plan.style``.
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, NodeStatus
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._media_selection_planning import (
    build_media_selection_context,
    materialize_media_selection_outputs,
    select_media_assignment,
)


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    boundary = state.require(ArtifactKind.plan_narration_boundary).payload or {}
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}
    retrieval_artifact = state.artifacts.get(ArtifactKind.plan_window_material_retrieval)
    retrieval = retrieval_artifact.payload if retrieval_artifact is not None else None

    agent_context = build_media_selection_context(
        request=state.request,
        material=material,
        narration=narration,
        boundary=boundary,
        windows=windows,
        retrieval=retrieval,
    )
    selection_result = select_media_assignment(ctx=ctx, agent_context=agent_context)
    materialized = materialize_media_selection_outputs(
        request=state.request,
        node_id=ctx.node_run.node_id,
        agent_context=agent_context,
        selection_result=selection_result,
    )

    return NodeOutput(
        status=NodeStatus.degraded if materialized.degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_media_assignment,
                materialized.assignment_payload,
                "MediaSelectionAssignmentPlan.v2",
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
                ArtifactKind.plan_media_selection_diagnostics,
                materialized.diagnostics,
                "MediaSelectionAgentDiagnostics.v1",
            ),
        ],
        warnings=materialized.warnings,
        degradations=materialized.degradations,
        provider_invocation_ids=selection_result.provider_invocation_ids,
    )
