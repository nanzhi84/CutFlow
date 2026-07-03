"""PortraitPlanning node: republish the compiler's default portrait plan."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    windows = ctx.state.require(ArtifactKind.plan_timeline_windows).payload or {}
    default_assignment = windows.get("default_assignment") or {}
    payload = default_assignment.get("portrait_plan_payload") or {}
    return NodeOutput(
        artifacts=[ctx.artifact(ArtifactKind.plan_portrait, payload, "PortraitPlanArtifact.v1")]
    )
