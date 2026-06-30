"""BrollCoveragePlanning node: cover the full narration with B-roll clips."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.contracts.artifacts import BrollOverlay, BrollPlanArtifact, NarrationUnit
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.material import (
    ScriptSegment,
    demote_recent_broll_candidates,
    extract_keywords,
    plan_coverage,
    rank_broll_candidates,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._broll_policy import (
    broll_generic_coverage_enabled,
    broll_recency_penalties,
)


def _narration_segments(units: list[NarrationUnit]) -> list[ScriptSegment]:
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
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    candidate_asset_ids = [
        item.get("asset_id")
        for item in material.get("broll_candidates", [])
        if isinstance(item, dict) and item.get("asset_id")
    ]

    narration = state.require(ArtifactKind.narration_units).payload or {}
    units = [NarrationUnit.model_validate(unit) for unit in narration.get("units", [])]
    segments = _narration_segments(units)

    audio = state.require(ArtifactKind.audio_tts)
    target_sec = float((audio.media_info.duration_sec if audio.media_info else 0) or 0)

    annotations = {
        asset_id: annotation
        for asset_id in dict.fromkeys(candidate_asset_ids)
        if (annotation := ctx.repository.annotation_v4_for_asset(asset_id)) is not None
    }
    # The ledger is NOT read here (MaterialPackPlanning is the single ledger-reading
    # node); recency is re-applied from the MaterialPack-computed penalties below.
    candidates = rank_broll_candidates(
        annotations=annotations,
        segments=segments,
        ledger_entries=(),
        include_generic_coverage=broll_generic_coverage_enabled(state.request),
    )
    penalty_by_clip, penalty_by_diversity = broll_recency_penalties(material)
    candidates = demote_recent_broll_candidates(
        candidates,
        penalty_by_clip=penalty_by_clip,
        penalty_by_diversity=penalty_by_diversity,
    )
    plan = plan_coverage(
        candidates=candidates,
        units=units,
        target_sec=target_sec,
        min_segment_duration=state.request.broll.min_segment_duration,
        freshness_seed=ctx.run.id,
    )
    if not plan.sufficient:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_broll,
            "B_roll material insufficient to cover the full narration duration.",
        )

    overlays = [
        BrollOverlay(
            overlay_id=f"broll_{index + 1}",
            asset_id=segment.asset_id,
            clip_id=segment.clip_id,
            timeline_start=segment.timeline_start,
            timeline_end=segment.timeline_end,
            source_start=segment.source_start,
            source_end=segment.source_end,
            reason=segment.reason,
            confidence=segment.confidence,
            matched_keywords=list(segment.matched_keywords),
            scene_name=segment.scene_name,
            diversity_key=segment.diversity_key or None,
        )
        for index, segment in enumerate(plan.segments)
    ]
    return NodeOutput(
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_broll,
                BrollPlanArtifact(
                    enabled=True,
                    overlays=overlays,
                ).model_dump(mode="json"),
                "BrollPlanArtifact.v1",
            )
        ]
    )
