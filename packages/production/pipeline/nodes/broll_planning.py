"""BrollPlanning node: place real b-roll inserts inside narration windows.

Real planning (no seeded ``start_sec = index * 3``): ranks the material pack's
annotated b-roll clips against the *real* narration beats (jieba keyword
similarity + usage-window coverage + recency demotion) and anchors each insert
inside the narration window it matched. When b-roll is enabled but no annotated
material clears the relevance floor, the node soft-degrades with
``broll.skipped_no_material`` (honest — never a fabricated pick).
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, NodeStatus, WarningCode
from packages.core.contracts.artifacts import BrollOverlay, BrollPlanArtifact, NarrationUnit
from packages.planning.material import (
    ScriptSegment,
    extract_keywords,
    plan_insertions,
    rank_broll_candidates,
)
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice


def _narration_segments(units: list[NarrationUnit]) -> list[ScriptSegment]:
    """Real narration beats as matchable script segments (text + true timing).

    Each beat carries its jieba-extracted keywords so the matcher can compute a
    real keyword overlap against the b-roll clip retrieval keywords.
    """
    return [
        ScriptSegment(
            text=unit.text,
            start=float(unit.start),
            end=float(unit.end),
            keywords=tuple(extract_keywords(unit.text)),
        )
        for unit in units
        if unit.end > unit.start
    ]


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    node_run = ctx.node_run

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

    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    candidate_asset_ids = [
        item.get("asset_id")
        for item in material.get("broll_candidates", [])
        if isinstance(item, dict) and item.get("asset_id")
    ]

    narration = state.require(ArtifactKind.narration_units).payload or {}
    units = [NarrationUnit.model_validate(unit) for unit in narration.get("units", [])]
    segments = _narration_segments(units)

    # Re-rank the candidate assets against the *real* narration beats so matched
    # keywords and the anchor beat come from true narration timing.
    annotations = {
        asset_id: annotation
        for asset_id in dict.fromkeys(candidate_asset_ids)
        if (annotation := ctx.repository.annotation_v4_for_asset(asset_id)) is not None
    }
    ledger_entries = ctx.repository.recent_selections(
        case_id=state.request.case_id, medium="broll"
    )
    candidates = rank_broll_candidates(
        annotations=annotations,
        segments=segments,
        ledger_entries=ledger_entries,
    )
    insertions = plan_insertions(
        candidates=candidates,
        units=units,
        max_inserts=state.request.broll.max_inserts,
    )

    if not insertions:
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
                    "No annotated b-roll material matched the narration.",
                    node_id=node_run.node_id,
                    affects_true_yield=True,
                )
            ],
        )

    segments_payload = [
        {
            "asset_id": ins.asset_id,
            "start_sec": ins.timeline_start,
            "end_sec": ins.timeline_end,
            "source_start": ins.source_start,
            "source_end": ins.source_end,
            "reason": ins.reason,
            "confidence": ins.confidence,
            "matched_keywords": list(ins.matched_keywords),
            "scene_name": ins.scene_name,
        }
        for ins in insertions
    ]
    overlays = [
        BrollOverlay(
            overlay_id=f"broll_{index + 1}",
            asset_id=ins.asset_id,
            timeline_start=ins.timeline_start,
            timeline_end=ins.timeline_end,
            source_start=ins.source_start,
            source_end=ins.source_end,
            reason=ins.reason,
            confidence=ins.confidence,
            matched_keywords=list(ins.matched_keywords),
            scene_name=ins.scene_name,
        )
        for index, ins in enumerate(insertions)
    ]
    return NodeOutput(
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_broll,
                BrollPlanArtifact(
                    enabled=True,
                    segments=segments_payload,
                    overlays=overlays,
                ).model_dump(mode="json"),
                "BrollPlanArtifact.v1",
            )
        ]
    )
