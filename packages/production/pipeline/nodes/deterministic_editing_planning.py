"""DeterministicEditingPlanning: consume per-window retrieval topK for v2."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode, NodeStatus, WarningCode
from packages.core.contracts.artifacts import BrollPlanArtifact, MediaAssignmentPlan
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing.frame_grid import frame_index
from packages.planning.material import longest_clean_portrait_source_span
from packages.production.pipeline._editing_agent import index_candidates
from packages.production.pipeline._materialize import (
    materialize_broll_from_assignment,
    materialize_portrait_from_assignment,
    materialize_style_from_selection,
    portrait_cut_frames,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline.nodes._creative_intent import load_creative_intent
from packages.production.pipeline.nodes.style_planning import _derive_overlay_events


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
    broll_assignment = _assign_broll_from_retrieval(
        retrieval=retrieval,
        candidates=candidates.broll_by_id,
        max_inserts=state.request.broll.max_inserts,
    )
    assignment = {"portrait": portrait_assignment, "broll": broll_assignment}
    broll_payload, broll_drops = materialize_broll_from_assignment(
        windows=windows,
        assignment=assignment,
        candidates=candidates,
        cut_frames=portrait_cut_frames(portrait_payload),
        enabled=state.request.broll.enabled,
        max_inserts=state.request.broll.max_inserts,
    )
    broll_degradations = []
    broll_warnings = []
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

    overlay_events = _derive_overlay_events(load_creative_intent(state).emphasis, units)
    style_payload, style_warnings, style_degradations = materialize_style_from_selection(
        request=state.request,
        material=material,
        overlay_events=overlay_events,
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
    max_inserts: int,
) -> list[dict]:
    assignments: list[dict] = []
    used_candidate_ids: set[str] = set()
    used_asset_ids: set[str] = set()
    used_diversity: set[str] = set()
    candidates_by_window = retrieval.get("candidates_by_window") or {}
    for window_id in sorted(candidates_by_window):
        if len(assignments) >= max(0, max_inserts):
            break
        for retrieved in candidates_by_window.get(window_id) or []:
            if not isinstance(retrieved, dict):
                continue
            candidate_id = str(retrieved.get("candidate_id") or "")
            candidate = candidates.get(candidate_id)
            if candidate is None or candidate_id in used_candidate_ids:
                continue
            asset_id = str(candidate.get("asset_id") or "")
            if asset_id and asset_id in used_asset_ids:
                continue
            metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
            diversity_key = str(metadata.get("diversity_key") or "")
            if diversity_key and diversity_key in used_diversity:
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
