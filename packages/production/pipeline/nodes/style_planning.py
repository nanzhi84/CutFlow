"""StylePlanning node: subtitle/BGM/font style plan with degradations.

Caption Display v2 (issue #188) froze the deterministic chain out of huazi: this
node no longer derives any emphasis overlay events (``overlay_events`` is always
empty). Huazi is planned exclusively by the ``EditingAgentPlanning`` chain's
HuaziPlanningSubagent.
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, NodeStatus, normalize_bgm_mood
from packages.core.workflow import NodeOutput
from packages.production.pipeline._materialize import materialize_style_from_selection
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._creative_intent import load_creative_intent


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    node_run = ctx.node_run
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    creative_intent = load_creative_intent(state)
    payload, warnings, degradations = materialize_style_from_selection(
        request=state.request,
        material=material,
        overlay_events=[],
        target_bgm_mood=_target_bgm_mood(creative_intent.intent),
    )
    degradations = [
        notice.model_copy(update={"node_id": node_run.node_id}) for notice in degradations
    ]
    artifact = ctx.artifact(
        ArtifactKind.plan_style,
        payload,
        "StylePlanArtifact.v1",
    )
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[artifact],
        warnings=warnings,
        degradations=degradations,
    )


def _target_bgm_mood(intent: dict | None) -> str:
    if not isinstance(intent, dict):
        return ""
    return normalize_bgm_mood(intent.get("bgm_mood"))
