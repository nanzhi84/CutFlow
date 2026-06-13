"""BrollPlanning node: select b-roll inserts and their timeline windows."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, NodeStatus, WarningCode
from packages.core.contracts.artifacts import BrollPlanArtifact
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline._selection import candidate_keywords, candidate_scene_name


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    node_run = ctx.node_run
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    broll_candidates = [
        item for item in material.get("broll_candidates", [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    broll = [item.get("asset_id") for item in broll_candidates]
    candidate_by_id = {item["asset_id"]: item for item in broll_candidates}
    if not state.request.broll.enabled:
        return NodeOutput(
            artifacts=[
                ctx.artifact(
                    ArtifactKind.plan_broll,
                    BrollPlanArtifact(enabled=False, segments=[]).model_dump(mode="json"),
                    "BrollPlanArtifact.v1",
                )
            ]
        )
    if state.request.broll.enabled and not broll:
        artifact = ctx.artifact(
            ArtifactKind.plan_broll,
            BrollPlanArtifact(
                enabled=True,
                segments=[],
                skipped_reason=WarningCode.broll_skipped_no_material.value,
            ).model_dump(mode="json"),
            "BrollPlanArtifact.v1",
        )
        return NodeOutput(
            status=NodeStatus.degraded,
            artifacts=[artifact],
            degradations=[
                degradation_notice(
                    WarningCode.broll_skipped_no_material,
                    "No b-roll material available.",
                    node_id=node_run.node_id,
                    affects_true_yield=True,
                )
            ],
        )
    segments = []
    for index, asset_id in enumerate(broll[: state.request.broll.max_inserts]):
        start_sec = index * 3
        end_sec = start_sec + 2
        candidate = candidate_by_id.get(asset_id)
        segments.append(
            {
                "asset_id": asset_id,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "source_start": 0,
                "source_end": end_sec - start_sec,
                "reason": "seeded usable b-roll",
                "confidence": 1,
                "matched_keywords": candidate_keywords(candidate),
                "scene_name": candidate_scene_name(candidate),
            }
        )
    return NodeOutput(
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_broll,
                BrollPlanArtifact(
                    enabled=state.request.broll.enabled,
                    segments=segments,
                    overlays=[
                        {
                            "overlay_id": f"broll_{index + 1}",
                            "asset_id": segment["asset_id"],
                            "timeline_start": segment["start_sec"],
                            "timeline_end": segment["end_sec"],
                            "source_start": segment["source_start"],
                            "source_end": segment["source_end"],
                            "reason": segment["reason"],
                            "confidence": segment["confidence"],
                            "matched_keywords": segment["matched_keywords"],
                            "scene_name": segment["scene_name"],
                        }
                        for index, segment in enumerate(segments)
                    ],
                ).model_dump(mode="json"),
                "BrollPlanArtifact.v1",
            )
        ]
    )
