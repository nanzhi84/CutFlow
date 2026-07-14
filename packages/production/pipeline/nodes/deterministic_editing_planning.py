"""DeterministicEditingPlanning: consume per-window retrieval topK for v2."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode, NodeStatus, WarningCode
from packages.core.contracts.artifacts import BrollPlanArtifact, MediaAssignmentPlan, NarrationUnit
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing.frame_grid import frame_index
from packages.planning.material import (
    ScriptSegment,
    demote_recent_broll_candidates,
    extract_keywords,
    longest_clean_portrait_source_span,
    rank_broll_candidates,
)
from packages.production.pipeline._editing_agent import index_candidates
from packages.production.pipeline._materialize import (
    full_coverage_broll_coverage_gaps,
    materialize_broll_from_assignment,
    materialize_full_coverage_broll_from_assignment,
    materialize_portrait_from_assignment,
    materialize_style_from_selection,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline.nodes._broll_policy import (
    broll_full_coverage_enabled,
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


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    node_run = ctx.node_run
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}
    retrieval = state.require(ArtifactKind.plan_window_material_retrieval).payload or {}
    narration_units = state.artifacts.get(ArtifactKind.narration_units)
    units = (narration_units.payload or {}).get("units", []) if narration_units is not None else []
    candidates = index_candidates(material)

    retrieval_portrait_assignment = _assign_portrait_from_retrieval(
        retrieval=retrieval,
        candidates=candidates.portrait_by_id,
        windows=windows,
    )
    portrait_fallback_diagnostics: dict = {}
    if _missing_portrait_window_ids(windows, retrieval_portrait_assignment):
        portrait_assignment = _default_portrait_assignment(windows)
        portrait_payload = _default_portrait_payload(windows)
        portrait_fallback_diagnostics = {
            "portrait_assignment_source": "timeline_window_default",
            "missing_retrieval_window_ids": _missing_portrait_window_ids(
                windows,
                retrieval_portrait_assignment,
            ),
        }
    else:
        portrait_assignment = retrieval_portrait_assignment
        portrait_payload = materialize_portrait_from_assignment(
            windows=windows,
            assignment={"portrait": portrait_assignment, "broll": []},
            candidates=candidates,
        )
    _ensure_portrait_coverage(
        windows=windows,
        assignment=portrait_assignment,
        portrait_payload=portrait_payload,
    )
    broll_limit = _broll_assignment_limit(
        request=state.request,
        windows=windows,
    )
    broll_assignment = _assign_broll_from_retrieval(
        retrieval=retrieval,
        candidates=candidates.broll_by_id,
        windows=windows,
        max_inserts=broll_limit,
        allow_asset_diversity_reuse=broll_full_coverage_enabled(state.request),
        allow_multi_clip_windows=False,
    )
    broll_candidate_index = candidates
    broll_fallback_diagnostics: dict = {}
    if (
        state.request.broll.enabled
        and not broll_assignment
        and not _has_broll_retrieval_topk(retrieval=retrieval, windows=windows)
    ):
        fallback_assignment, fallback_candidate_index = _assign_broll_from_annotations(
            ctx=ctx,
            material=material,
            windows=windows,
            units=units,
            max_inserts=broll_limit,
            allow_asset_diversity_reuse=broll_full_coverage_enabled(state.request),
            allow_multi_clip_windows=False,
        )
        if fallback_assignment:
            broll_assignment = fallback_assignment
            broll_candidate_index = fallback_candidate_index
            broll_fallback_diagnostics = {
                "broll_assignment_source": "annotation_ranked_fallback",
                "missing_retrieval_broll": True,
            }
    assignment = {"portrait": portrait_assignment, "broll": broll_assignment}
    if broll_full_coverage_enabled(state.request):
        broll_payload, broll_drops = materialize_full_coverage_broll_from_assignment(
            windows=windows,
            assignment=assignment,
            candidates=broll_candidate_index,
            enabled=state.request.broll.enabled,
            max_inserts=broll_limit,
        )
    else:
        broll_payload, broll_drops = materialize_broll_from_assignment(
            windows=windows,
            assignment=assignment,
            candidates=broll_candidate_index,
            enabled=state.request.broll.enabled,
            max_inserts=broll_limit,
        )
    broll_degradations = []
    broll_warnings = []
    if broll_full_coverage_enabled(state.request):
        _ensure_full_coverage_broll(
            windows=windows,
            broll_payload=broll_payload,
            broll_drops=broll_drops,
        )
    if state.request.broll.enabled and not broll_payload.get("overlays"):
        broll_payload = BrollPlanArtifact(
            enabled=True,
            skipped_reason=WarningCode.broll_skipped_no_material.value,
        ).model_dump(mode="json")
        broll_degradations.append(
            degradation_notice(
                WarningCode.broll_skipped_no_material,
                "No retrieved b-roll candidate covered an authoritative B-roll window.",
                node_id=node_run.node_id,
                affects_true_yield=True,
            ).model_copy(
                update={
                    "details": {
                        "broll_drops": broll_drops,
                        "retrieval_window_count": len(retrieval.get("candidates_by_window") or {}),
                    }
                }
            )
        )
        broll_warnings.append(WarningCode.broll_skipped_no_material)
    elif broll_drops:
        broll_degradations.append(
            degradation_notice(
                WarningCode.broll_insertions_dropped_geometry,
                f"B-roll 有 {len(broll_drops)} 个候选未能覆盖权威窗口。",
                node_id=node_run.node_id,
                affects_true_yield=False,
            ).model_copy(update={"details": {"broll_drops": broll_drops}})
        )
        broll_warnings.append(WarningCode.broll_insertions_dropped_geometry)

    # Caption Display v2 (issue #188): the deterministic chain plans no huazi;
    # emphasis captions are an EditingAgentPlanning-chain-only capability.
    style_payload, style_warnings, style_degradations = materialize_style_from_selection(
        request=state.request,
        material=material,
        overlay_events=[],
    )
    style_degradations = [
        notice.model_copy(update={"node_id": node_run.node_id}) for notice in style_degradations
    ]
    media_assignment = MediaAssignmentPlan(
        engine="deterministic_default",
        portrait=portrait_assignment,
        broll=broll_assignment,
        diagnostics={
            "source": "window_material_retrieval",
            "portrait_segment_count": len(portrait_payload.get("segments") or []),
            **portrait_fallback_diagnostics,
            **broll_fallback_diagnostics,
            "broll_drops": broll_drops,
            "retrieval_diagnostics": retrieval.get("diagnostics") or {},
        },
    ).model_dump(mode="json")

    degradations = [*broll_degradations, *style_degradations]
    warnings = [*broll_warnings, *style_warnings]
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_media_assignment,
                media_assignment,
                "MediaAssignmentPlan.v1",
            ),
            ctx.artifact(ArtifactKind.plan_portrait, portrait_payload, "PortraitPlanArtifact.v1"),
            ctx.artifact(ArtifactKind.plan_broll, broll_payload, "BrollPlanArtifact.v1"),
            ctx.artifact(ArtifactKind.plan_style, style_payload, "StylePlanArtifact.v1"),
        ],
        warnings=warnings,
        degradations=degradations,
    )


def _assign_portrait_from_retrieval(
    *,
    retrieval: dict,
    candidates: dict[str, dict],
    windows: dict,
) -> list[dict]:
    assignments: list[dict] = []
    candidates_by_window = retrieval.get("candidates_by_window") or {}
    used_asset_ids: set[str] = set()
    portrait_windows = sorted(
        (
            window
            for window in (windows.get("portrait_windows") or [])
            if isinstance(window, dict)
        ),
        key=lambda window: int(window.get("start_frame", 0) or 0),
    )
    for window in portrait_windows:
        window_id = str(window.get("window_id") or "")
        required_frames = _required_frames(window)
        for retrieved in candidates_by_window.get(window_id) or []:
            if not isinstance(retrieved, dict):
                continue
            candidate_id = str(retrieved.get("candidate_id") or "")
            candidate = candidates.get(candidate_id)
            if candidate is None:
                continue
            asset_id = str(candidate.get("asset_id") or "")
            if asset_id and asset_id in used_asset_ids:
                continue
            if _portrait_source_frames_available(candidate) < required_frames:
                continue
            if asset_id:
                used_asset_ids.add(asset_id)
            assignments.append(
                {
                    "window_id": window_id,
                    "candidate_id": candidate_id,
                    "source_mode": "lipsynced",
                    "reason": "window retrieval topK",
                }
            )
            break
    return assignments


def _default_portrait_assignment(windows: dict) -> list[dict]:
    default_assignment = windows.get("default_assignment") or {}
    defaults = [
        item for item in (default_assignment.get("portrait") or []) if isinstance(item, dict)
    ]
    portrait_windows = [
        item for item in (windows.get("portrait_windows") or []) if isinstance(item, dict)
    ]
    assignment: list[dict] = []
    for window_data, default in zip(portrait_windows, defaults):
        segment_payload = default.get("segment_payload") or {}
        assignment.append(
            {
                "window_id": str(window_data.get("window_id") or ""),
                "candidate_id": str(default.get("window_id") or ""),
                "source_mode": str(segment_payload.get("source_mode") or "lipsynced"),
                "reason": "compiler default",
            }
        )
    return assignment


def _default_portrait_payload(windows: dict) -> dict:
    default_assignment = windows.get("default_assignment") or {}
    return dict(default_assignment.get("portrait_plan_payload") or {})


def _missing_portrait_window_ids(windows: dict, assignment: list[dict]) -> list[str]:
    expected = {
        str(window.get("window_id") or "")
        for window in (windows.get("portrait_windows") or [])
        if isinstance(window, dict) and str(window.get("window_id") or "")
    }
    assigned = {
        str(item.get("window_id") or "")
        for item in assignment
        if isinstance(item, dict) and str(item.get("window_id") or "")
    }
    return sorted(expected - assigned)


def _assign_broll_from_retrieval(
    *,
    retrieval: dict,
    candidates: dict[str, dict],
    windows: dict,
    max_inserts: int,
    allow_asset_diversity_reuse: bool = False,
    allow_multi_clip_windows: bool = False,
) -> list[dict]:
    assignments: list[dict] = []
    used_candidate_ids: set[str] = set()
    used_asset_ids: set[str] = set()
    used_diversity: set[str] = set()
    candidates_by_window = retrieval.get("candidates_by_window") or {}
    broll_windows = sorted(
        (
            window
            for window in (windows.get("broll_windows") or [])
            if isinstance(window, dict) and str(window.get("window_id") or "")
        ),
        key=lambda window: int(window.get("start_frame", 0) or 0),
    )
    window_ids = [str(window.get("window_id") or "") for window in broll_windows]
    if not window_ids:
        window_ids = sorted(candidates_by_window)
    required_by_window = {
        str(window.get("window_id") or ""): _required_frames(window)
        for window in broll_windows
    }
    for window_id in window_ids:
        if len(assignments) >= max(0, max_inserts):
            break
        covered_frames = 0
        required_frames = int(required_by_window.get(window_id, 0) or 0)
        for retrieved in candidates_by_window.get(window_id) or []:
            if not isinstance(retrieved, dict):
                continue
            candidate_id = str(retrieved.get("candidate_id") or "")
            candidate = candidates.get(candidate_id)
            if candidate is None or candidate_id in used_candidate_ids:
                continue
            asset_id = str(candidate.get("asset_id") or "")
            if not allow_asset_diversity_reuse and asset_id and asset_id in used_asset_ids:
                continue
            metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
            diversity_key = str(metadata.get("diversity_key") or "")
            if (
                not allow_asset_diversity_reuse
                and diversity_key
                and diversity_key in used_diversity
            ):
                continue
            source_frames = _broll_source_frames_available(candidate)
            if source_frames <= 0:
                continue
            if not allow_multi_clip_windows and source_frames < required_frames:
                continue
            used_candidate_ids.add(candidate_id)
            if asset_id:
                used_asset_ids.add(asset_id)
            if diversity_key:
                used_diversity.add(diversity_key)
            assignments.append(
                {
                    "window_id": window_id,
                    "candidate_id": candidate_id,
                    "reason": "window retrieval topK",
                    "confidence": float(retrieved.get("retrieval_score") or 0.0),
                    "matched_keywords": list(metadata.get("matched_keywords") or []),
                }
            )
            covered_frames += source_frames
            if not allow_multi_clip_windows or covered_frames >= required_frames:
                break
    return assignments


def _has_broll_retrieval_topk(*, retrieval: dict, windows: dict) -> bool:
    broll_window_ids = {
        str(window.get("window_id") or "")
        for window in (windows.get("broll_windows") or [])
        if isinstance(window, dict) and str(window.get("window_id") or "")
    }
    candidates_by_window = retrieval.get("candidates_by_window") or {}
    for window_id in broll_window_ids:
        topk = candidates_by_window.get(window_id) or []
        if any(
            isinstance(retrieved, dict) and str(retrieved.get("candidate_id") or "")
            for retrieved in topk
        ):
            return True
    return False


def _assign_broll_from_annotations(
    *,
    ctx: NodeContext,
    material: dict,
    windows: dict,
    units: list[dict],
    max_inserts: int,
    allow_asset_diversity_reuse: bool = False,
    allow_multi_clip_windows: bool = False,
) -> tuple[list[dict], dict]:
    candidate_asset_ids = [
        item.get("asset_id")
        for item in material.get("broll_candidates", [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    narration_units = [NarrationUnit.model_validate(unit) for unit in units]
    segments = _narration_segments(narration_units)
    annotations = {
        asset_id: annotation
        for asset_id in dict.fromkeys(candidate_asset_ids)
        if (annotation := ctx.repository.annotation_v4_for_asset(asset_id)) is not None
    }
    ranked_candidates = rank_broll_candidates(
        annotations=annotations,
        segments=segments,
        ledger_entries=(),
        include_generic_coverage=broll_generic_coverage_enabled(ctx.state.request),
    )
    penalty_by_clip, penalty_by_diversity = broll_recency_penalties(material)
    ranked_candidates = demote_recent_broll_candidates(
        ranked_candidates,
        penalty_by_clip=penalty_by_clip,
        penalty_by_diversity=penalty_by_diversity,
    )
    candidate_index = _indexed_broll_candidates(ranked_candidates)
    assignment = _assign_indexed_broll_candidates_to_windows(
        windows=windows,
        candidates=candidate_index["broll_by_id"],
        max_inserts=max_inserts,
        allow_asset_diversity_reuse=allow_asset_diversity_reuse,
        allow_multi_clip_windows=allow_multi_clip_windows,
    )
    return assignment, candidate_index


def _assign_indexed_broll_candidates_to_windows(
    *,
    windows: dict,
    candidates: dict[str, dict],
    max_inserts: int,
    allow_asset_diversity_reuse: bool = False,
    allow_multi_clip_windows: bool = False,
) -> list[dict]:
    assignments: list[dict] = []
    used_candidate_ids: set[str] = set()
    used_asset_ids: set[str] = set()
    used_diversity: set[str] = set()
    broll_windows = [
        window for window in (windows.get("broll_windows") or []) if isinstance(window, dict)
    ]
    for window in broll_windows:
        if len(assignments) >= max(0, max_inserts):
            break
        required_frames = _required_frames(window)
        covered_frames = 0
        for candidate_id, candidate in candidates.items():
            metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
            asset_id = str(candidate.get("asset_id") or "")
            diversity_key = str(metadata.get("diversity_key") or "")
            if candidate_id in used_candidate_ids:
                continue
            if not allow_asset_diversity_reuse and asset_id and asset_id in used_asset_ids:
                continue
            if (
                not allow_asset_diversity_reuse
                and diversity_key
                and diversity_key in used_diversity
            ):
                continue
            source_frames = _broll_source_frames_available(candidate)
            if source_frames <= 0:
                continue
            if not allow_multi_clip_windows and source_frames < required_frames:
                continue
            used_candidate_ids.add(candidate_id)
            if asset_id:
                used_asset_ids.add(asset_id)
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
            covered_frames += source_frames
            if not allow_multi_clip_windows or covered_frames >= required_frames:
                break
    return assignments


def _ensure_portrait_coverage(
    *,
    windows: dict,
    assignment: list[dict],
    portrait_payload: dict,
) -> None:
    portrait_windows = [
        window for window in (windows.get("portrait_windows") or []) if isinstance(window, dict)
    ]
    expected_window_ids = {
        str(window.get("window_id") or "") for window in portrait_windows if window.get("window_id")
    }
    assigned_window_ids = {
        str(item.get("window_id") or "") for item in assignment if isinstance(item, dict)
    }
    segment_count = len(
        [segment for segment in (portrait_payload.get("segments") or []) if isinstance(segment, dict)]
    )
    missing_assignment_ids = sorted(expected_window_ids - assigned_window_ids)
    missing_segment_count = max(0, len(expected_window_ids) - segment_count)
    if missing_assignment_ids or missing_segment_count:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "人像素材不足：retrieval topK 未覆盖所有 portrait window。",
            details={
                "missing_assignment_window_ids": missing_assignment_ids,
                "expected_portrait_window_count": len(expected_window_ids),
                "assigned_portrait_window_count": len(assigned_window_ids),
                "materialized_portrait_segment_count": segment_count,
            },
        )


def _broll_assignment_limit(*, request, windows: dict) -> int:
    if broll_full_coverage_enabled(request):
        window_count = len([w for w in (windows.get("broll_windows") or []) if isinstance(w, dict)])
        return window_count
    return request.broll.max_inserts


def _ensure_full_coverage_broll(
    *,
    windows: dict,
    broll_payload: dict,
    broll_drops: list[dict],
) -> None:
    expected = {
        str(window.get("window_id") or "")
        for window in (windows.get("broll_windows") or [])
        if isinstance(window, dict) and str(window.get("window_id") or "")
    }
    covered = {
        str(overlay.get("window_id") or "")
        for overlay in (broll_payload.get("overlays") or [])
        if isinstance(overlay, dict) and str(overlay.get("window_id") or "")
    }
    missing = sorted(expected - covered)
    coverage_gaps = full_coverage_broll_coverage_gaps(
        windows=windows,
        overlays=[overlay for overlay in (broll_payload.get("overlays") or []) if isinstance(overlay, dict)],
    )
    if missing or coverage_gaps or broll_drops:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_broll,
            "B-roll full coverage requires every authoritative window to have material.",
            details={
                "missing_broll_window_ids": missing,
                "expected_broll_window_count": len(expected),
                "covered_broll_window_count": len(covered),
                "coverage_gaps": coverage_gaps,
                "broll_drops": broll_drops,
            },
        )


def _required_frames(window: dict) -> int:
    start = int(window.get("start_frame", 0) or 0)
    end = int(window.get("end_frame", 0) or 0)
    return max(0, int(window.get("length_frames") or end - start))


def _portrait_source_frames_available(candidate: dict) -> int:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    clean_span = longest_clean_portrait_source_span(metadata)
    if clean_span is None:
        return 0
    source_start, source_end = clean_span
    return max(0, frame_index(source_end) - frame_index(source_start))


def _broll_source_frames_available(candidate: dict) -> int:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    try:
        source_start = float(metadata.get("source_start") or 0.0)
        source_end = float(metadata.get("source_end") or 0.0)
    except (TypeError, ValueError):
        return 0
    return max(0, frame_index(source_end) - frame_index(source_start))
