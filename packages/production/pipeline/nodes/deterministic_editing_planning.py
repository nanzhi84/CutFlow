"""DeterministicEditingPlanning: consume per-window retrieval topK for v2."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, NodeStatus, WarningCode
from packages.core.contracts.artifacts import BrollPlanArtifact, MediaAssignmentPlan
from packages.core.workflow import NodeOutput
from packages.production.pipeline._editing_agent import index_candidates
from packages.production.pipeline._materialize import (
    materialize_broll_from_assignment,
    materialize_style_from_selection,
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

    broll_assignment = _assign_broll_from_retrieval(
        retrieval=retrieval,
        candidates=candidates.broll_by_id,
        max_inserts=state.request.broll.max_inserts,
    )
    assignment = {"portrait": [], "broll": broll_assignment}
    broll_payload, broll_drops = materialize_broll_from_assignment(
        windows=windows,
        assignment=assignment,
        candidates=candidates,
        cut_frames=[],
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
        broll=broll_assignment,
        diagnostics={
            "source": "window_material_retrieval",
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
            ctx.artifact(ArtifactKind.plan_broll, broll_payload, "BrollPlanArtifact.v1"),
            ctx.artifact(ArtifactKind.plan_style, style_payload, "StylePlanArtifact.v1"),
        ],
        warnings=warnings,
        degradations=degradations,
    )


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
