"""BrollPlanning node: bind b-roll candidates to authoritative windows.

Real planning (no seeded ``start_sec = index * 3``): ranks the material pack's
annotated b-roll clips against the *real* narration beats (jieba keyword
similarity + usage-window coverage + recency demotion), then binds ranked
candidates to ``TimelineWindowPlanning``'s fixed optional B-roll windows. It does
not add, move, resize, or snap those windows. When b-roll is enabled but no
annotated material can cover an authoritative window, the node soft-degrades with
``broll.skipped_no_material`` (honest — never a fabricated pick).
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, NodeStatus, WarningCode
from packages.core.contracts.artifacts import BrollPlanArtifact, NarrationUnit
from packages.planning.editing.frame_grid import frame_index
from packages.planning.material import (
    ScriptSegment,
    demote_recent_broll_candidates,
    extract_keywords,
    rank_broll_candidates,
)
from packages.core.workflow import NodeOutput
from packages.production.pipeline._materialize import materialize_broll_from_assignment
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline.nodes._broll_policy import (
    broll_generic_coverage_enabled,
    broll_recency_penalties,
)


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
                    BrollPlanArtifact(enabled=False).model_dump(mode="json"),
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

    # TimelineWindowPlanning owns the optional B-roll placement slots. This node may
    # pick/skip candidates for those windows, but it must not invent or snap geometry.
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}

    # Re-rank the candidate assets against the *real* narration beats so matched
    # keywords and the anchor beat come from true narration timing. The ledger is NOT
    # read here (MaterialPackPlanning is the single ledger-reading node); recency is
    # re-applied from the MaterialPack-computed penalties below.
    annotations = {
        asset_id: annotation
        for asset_id in dict.fromkeys(candidate_asset_ids)
        if (annotation := ctx.repository.annotation_v4_for_asset(asset_id)) is not None
    }
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
    indexed_candidates = _indexed_broll_candidates(candidates)
    assignment = {
        "broll": _assign_candidates_to_windows(
            windows=windows,
            candidates=indexed_candidates["broll_by_id"],
            max_inserts=state.request.broll.max_inserts,
        )
    }
    broll_payload, broll_drops = materialize_broll_from_assignment(
        windows=windows,
        assignment=assignment,
        candidates=indexed_candidates,
        cut_frames=[],
        enabled=True,
        max_inserts=state.request.broll.max_inserts,
    )

    if not broll_payload.get("overlays"):
        artifact = ctx.artifact(
            ArtifactKind.plan_broll,
            BrollPlanArtifact(
                enabled=True,
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
                    "No annotated b-roll material matched an authoritative B-roll window.",
                    node_id=node_run.node_id,
                    affects_true_yield=True,
                ).model_copy(update={"details": {"broll_drops": broll_drops}}),
            ],
        )

    artifacts = [
        ctx.artifact(
            ArtifactKind.plan_broll,
            broll_payload,
            "BrollPlanArtifact.v1",
        )
    ]
    if broll_drops:
        return NodeOutput(
            status=NodeStatus.degraded,
            artifacts=artifacts,
            degradations=[
                degradation_notice(
                    WarningCode.broll_insertions_dropped_geometry,
                    f"B-roll 有 {len(broll_drops)} 个候选未能覆盖权威窗口。",
                    node_id=node_run.node_id,
                    affects_true_yield=False,
                )
                .model_copy(update={"details": {"broll_drops": broll_drops}})
            ],
        )

    return NodeOutput(artifacts=artifacts)


def _indexed_broll_candidates(candidates) -> dict[str, dict[str, dict]]:
    return {
        "broll_by_id": {
            f"bc_{index:03d}": {
                "asset_id": candidate.asset_id,
                "score": candidate.score,
                "reason": (
                    f"matched '{candidate.scene_name}' (base {candidate.base_score})"
                    + ("; recently used" if candidate.recency_penalty else "")
                ),
                "metadata": {
                    "clip_id": candidate.clip_id,
                    "source_start": candidate.source_start,
                    "source_end": candidate.source_end,
                    "matched_keywords": list(candidate.matched_keywords),
                    "scene_name": candidate.scene_name,
                    "base_score": candidate.base_score,
                    "recency_penalty": candidate.recency_penalty,
                    "diversity_key": candidate.diversity_key,
                },
            }
            for index, candidate in enumerate(candidates)
        }
    }


def _assign_candidates_to_windows(
    *,
    windows: dict,
    candidates: dict[str, dict],
    max_inserts: int,
) -> list[dict]:
    assignments: list[dict] = []
    used_candidates: set[str] = set()
    used_assets: set[str] = set()
    used_diversity: set[str] = set()
    broll_windows = [
        window for window in (windows.get("broll_windows") or []) if isinstance(window, dict)
    ]
    for window in broll_windows[: max(0, max_inserts)]:
        required_frames = _window_required_frames(window)
        selected = None
        for candidate_id, candidate in candidates.items():
            metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
            asset_id = str(candidate.get("asset_id") or "")
            diversity_key = str(metadata.get("diversity_key") or "")
            if candidate_id in used_candidates:
                continue
            if asset_id and asset_id in used_assets:
                continue
            if diversity_key and diversity_key in used_diversity:
                continue
            if _source_frames_available(candidate) < required_frames:
                continue
            selected = (candidate_id, candidate, metadata, asset_id, diversity_key)
            break
        if selected is None:
            continue
        candidate_id, candidate, metadata, asset_id, diversity_key = selected
        used_candidates.add(candidate_id)
        if asset_id:
            used_assets.add(asset_id)
        if diversity_key:
            used_diversity.add(diversity_key)
        assignments.append(
            {
                "window_id": str(window.get("window_id") or ""),
                "candidate_id": candidate_id,
                "reason": str(candidate.get("reason") or "deterministic window assignment"),
                "confidence": float(candidate.get("score") or 0.0),
                "matched_keywords": list(metadata.get("matched_keywords") or []),
            }
        )
    return assignments


def _window_required_frames(window: dict) -> int:
    start = int(window.get("start_frame", 0) or 0)
    end = int(window.get("end_frame", 0) or 0)
    return int(window.get("length_frames") or max(0, end - start))


def _source_frames_available(candidate: dict) -> int:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    try:
        source_start = float(metadata.get("source_start") or 0.0)
        source_end = float(metadata.get("source_end") or 0.0)
    except (TypeError, ValueError):
        return 0
    return max(0, frame_index(source_end) - frame_index(source_start))
