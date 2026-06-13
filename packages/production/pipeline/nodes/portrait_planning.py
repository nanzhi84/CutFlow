"""PortraitPlanning node: plan the main portrait track covering the narration."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.contracts.artifacts import PortraitPlanArtifact
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    portraits = [item.get("asset_id") for item in material.get("portrait_candidates", []) if item.get("asset_id")]
    if state.request.strictness.portrait_insufficient_policy == "hard_fail" and not portraits:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "Portrait main track cannot cover the full audio.",
        )
    duration = max([float(unit.get("end", 0)) for unit in narration.get("units", [])] or [1])
    asset_id = portraits[0] if portraits else None
    source_artifact = ctx.source_artifact_for_asset(asset_id) if asset_id else None
    source_duration = (
        float(source_artifact.media_info.duration_sec or 0)
        if source_artifact and source_artifact.media_info
        else 0
    )
    if asset_id and source_duration + (1 / state.request.output.fps) < duration:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "Portrait source window cannot cover the full audio.",
        )
    payload = PortraitPlanArtifact(
        fps=state.request.output.fps,
        total_duration=duration,
        asset_id=asset_id,
        duration_sec=duration,
        segments=[
            {
                "asset_id": asset_id,
                "start_sec": 0,
                "end_sec": duration,
                "source_start": 0,
                "source_end": duration,
                "role": "main",
                "unit_ids": [unit.get("unit_id") for unit in narration.get("units", [])],
            }
        ],
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[ctx.artifact(ArtifactKind.plan_portrait, payload, "PortraitPlanArtifact.v1")]
    )
