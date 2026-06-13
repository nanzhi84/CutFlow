"""Capacity packing: assign portrait source windows to boundary-locked chunks.

Ported from editing_agent/boundary_planning.py (the capacity half). After chunk
durations are known, pick portrait source windows so every cut lands on a boundary
and no window is over-extended. Strategy (origin's calibration, faithful):
  - candidate scopes: fresh-unique -> fresh-limited-reuse -> all-penalty ->
    capacity-fallback-all (each with its own scope_penalty + relax passes);
  - per scope, try chunk variants (rhythm / preserve-tail / inventory caps), tier 0
    first; within a tier keep the highest-scoring feasible plan;
  - per relax pass, run the fixed-width beam; if every pass fails but coverage is
    achievable, run the backtracking rescue.
Output segments carry timeline + source spans (seconds); the FINAL frame
quantization to the single 30fps grid happens in :mod:`packages.planning.editing.plan`.
"""

from __future__ import annotations

import math
from typing import Any

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.editing import _candidate, _util as util
from packages.planning.editing import chunks as chunks_mod
from packages.planning.editing.beam import assign_boundary_windows_beam
from packages.planning.editing.constants import (
    ADJACENCY_PENALTY,
    BOUNDARY_BEAM_WIDTH,
    DEFAULT_BRANCH_FACTOR,
)
from packages.planning.editing.rescue import rescue_boundary_assignment_with_backtracking
from packages.planning.editing.segments import (
    portrait_boundary_candidate_scopes,
    selected_segments_from_assignment,
)

__all__ = [
    "portrait_boundary_candidate_scopes",
    "assign_boundary_windows_for_chunks",
    "build_boundary_locked_portrait_plan",
]


def assign_boundary_windows_for_chunks(
    *,
    chunks: list[dict[str, Any]],
    portrait_candidates: list[dict[str, Any]],
    target_duration: float,
    pause_windows: list[dict[str, float]] | None = None,
    variant: str,
    candidate_scope: str,
    variant_penalty: float = 0.0,
    scope_penalty: float = 0.0,
    relax_passes: list[dict[str, Any]] | None = None,
    ignore_repetition_penalty: bool = False,
    beam_width: int = BOUNDARY_BEAM_WIDTH,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]], float]:
    active_relax_passes = list(
        relax_passes
        or [
            {"allow_adjacent": True, "max_uses": 999, "allow_original": False},
            {"allow_adjacent": True, "max_uses": 999, "allow_original": True},
        ]
    )
    trace: list[dict[str, Any]] = [
        {
            "strategy": "portrait_boundary_react_planner",
            "action": "observe_capacity",
            "variant": variant,
            "candidate_scope": candidate_scope,
            "chunk_count": len(chunks or []),
            "candidate_count": len(portrait_candidates or []),
            "relax_pass_count": len(active_relax_passes),
        }
    ]
    if not chunks or not portrait_candidates:
        trace.append(
            {
                "strategy": "portrait_boundary_capacity_planner",
                "status": "skipped",
                "variant": variant,
                "candidate_scope": candidate_scope,
                "reason": "缺少可用候选或剪辑边界。",
            }
        )
        return None, trace, float("-inf")

    const_penalty = variant_penalty + scope_penalty

    for relax_level, relax in enumerate(active_relax_passes):
        score_fn = _candidate.make_boundary_beam_score_fn(
            relax=relax, ignore_repetition_penalty=ignore_repetition_penalty
        )
        best_assignment, beam_score = assign_boundary_windows_beam(
            chunks=chunks,
            candidates=portrait_candidates,
            beam_width=int(beam_width),
            branch_factor=DEFAULT_BRANCH_FACTOR,
            score_fn=score_fn,
            round_time=util.round_time,
            adjacency_penalty=ADJACENCY_PENALTY,
        )
        if not best_assignment:
            continue
        best_score = beam_score + const_penalty
        selected = selected_segments_from_assignment(
            chunks=chunks,
            assignment=best_assignment,
            candidate_scope=candidate_scope,
            pause_windows=pause_windows,
        )
        coverage = sum(util.as_float(seg.get("duration"), 0.0) for seg in selected)
        if coverage + 0.08 < util.round_time(target_duration):
            continue
        trace.append(
            {
                "strategy": "portrait_boundary_react_planner",
                "status": "candidate_plan_passed",
                "variant": variant,
                "candidate_scope": candidate_scope,
                "relax_level": relax_level,
                "score": round(best_score, 3),
            }
        )
        return selected, trace, best_score

    # Every beam relax pass failed: fixed-width beam can spend a scarce window early
    # and miss a feasible packing. Run the backtracking rescue with the loosest pass
    # — but only when coverage is even achievable (chunk total >= target).
    rescue_attempted = False
    rescue_meta: dict[str, Any] = {}
    coverage_total = sum(util.round_time(chunk.get("duration", 0.0)) for chunk in chunks)
    if active_relax_passes and coverage_total + 0.08 >= util.round_time(target_duration):
        rescue_attempted = True
        rescue_relax = active_relax_passes[-1]
        rescue_score_fn = _candidate.make_boundary_beam_score_fn(
            relax=rescue_relax, ignore_repetition_penalty=ignore_repetition_penalty
        )
        rescue_assignment, rescue_total, rescue_meta = rescue_boundary_assignment_with_backtracking(
            chunks=chunks,
            candidates=portrait_candidates,
            score_fn=rescue_score_fn,
            round_time=util.round_time,
            adjacency_penalty=ADJACENCY_PENALTY,
        )
        if rescue_assignment:
            best_score = rescue_total + const_penalty
            selected = selected_segments_from_assignment(
                chunks=chunks,
                assignment=rescue_assignment,
                candidate_scope=candidate_scope,
                pause_windows=pause_windows,
            )
            trace.append(
                {
                    "strategy": "portrait_boundary_feasibility_backtracking_rescue",
                    "status": "rescued",
                    "variant": variant,
                    "candidate_scope": candidate_scope,
                    "score": round(best_score, 3),
                    "rescue_nodes_explored": int(rescue_meta.get("nodes_explored", 0)),
                }
            )
            return selected, trace, best_score

    trace.append(
        {
            "strategy": "portrait_boundary_capacity_planner",
            "status": "failed",
            "variant": variant,
            "candidate_scope": candidate_scope,
            "rescue_attempted": rescue_attempted,
            "rescue_timeout": bool(rescue_meta.get("timed_out")),
            "rescue_nodes_explored": int(rescue_meta.get("nodes_explored", 0)),
            "reason": "没有找到能全局覆盖所有气口/句尾区间的唯一人像窗口组合。",
        }
    )
    return None, trace, float("-inf")


def build_boundary_locked_portrait_plan(
    *,
    portrait_candidates: list[dict[str, Any]],
    narration_units: list[NarrationUnit],
    target_duration: float,
    pause_windows: list[dict[str, float]] | None = None,
    max_chunk_duration: float | None = None,
    include_unlimited_reuse_scope: bool = True,
    beam_width: int = BOUNDARY_BEAM_WIDTH,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Select portrait windows after boundary durations are known.

    Cuts never require over-extending a source: a window is only assigned to a chunk
    if it can cover the whole boundary interval. Returns (plan_segments | None, trace).
    """
    cap_durations: list[float] | None = None
    if max_chunk_duration is not None:
        cap_durations = chunks_mod.derive_capacity_cap_durations(
            portrait_candidates, max_chunk_duration=max_chunk_duration
        )
    variants = chunks_mod.boundary_chunk_variants(
        narration_units=narration_units,
        target_duration=target_duration,
        pause_windows=pause_windows,
        max_chunk_duration=max_chunk_duration,
        cap_durations=cap_durations,
    )
    if not variants:
        return None, [
            {
                "strategy": "portrait_boundary_capacity_planner",
                "status": "skipped",
                "reason": "缺少可用候选或剪辑边界。",
            }
        ]

    best_plan: list[dict[str, Any]] | None = None
    best_score = float("-inf")
    best_variant: dict[str, Any] = variants[0]
    attempt_trace: list[dict[str, Any]] = []
    scopes = portrait_boundary_candidate_scopes(
        portrait_candidates, include_unlimited_reuse_scope=include_unlimited_reuse_scope
    )
    for scope in scopes:
        scope_best_plan: list[dict[str, Any]] | None = None
        scope_best_score = float("-inf")
        scope_best_variant: dict[str, Any] = variants[0]
        scope_best_tier = math.inf
        scope_attempt_trace: list[dict[str, Any]] = []
        for variant in variants:
            variant_tier = int(variant.get("variant_tier", 0) or 0)
            if scope_best_plan is not None and variant_tier > scope_best_tier:
                continue
            chunks = list(variant.get("chunks") or [])
            plan, trace_rows, score = assign_boundary_windows_for_chunks(
                chunks=chunks,
                portrait_candidates=list(scope.get("candidates") or []),
                target_duration=target_duration,
                pause_windows=pause_windows,
                variant=str(variant.get("variant") or "unknown"),
                candidate_scope=str(scope.get("scope") or "all"),
                variant_penalty=util.as_float(variant.get("variant_penalty"), 0.0),
                scope_penalty=util.as_float(scope.get("scope_penalty"), 0.0),
                relax_passes=list(scope.get("relax_passes") or []),
                ignore_repetition_penalty=bool(scope.get("ignore_repetition_penalty", False)),
                beam_width=beam_width,
            )
            scope_attempt_trace.extend(trace_rows)
            if plan and (
                variant_tier < scope_best_tier
                or (variant_tier == scope_best_tier and score > scope_best_score)
            ):
                scope_best_plan = plan
                scope_best_score = score
                scope_best_variant = variant
                scope_best_tier = variant_tier
        attempt_trace.extend(scope_attempt_trace)
        if scope_best_plan:
            best_plan = scope_best_plan
            best_score = scope_best_score
            best_variant = scope_best_variant
            break

    trace: list[dict[str, Any]] = [
        {
            "strategy": "portrait_boundary_capacity_planner",
            "boundary_policy": "semantic_boundary_audio_pause_capacity_locked"
            if pause_windows
            else "semantic_boundary_capacity_locked",
            "selected_variant": best_variant.get("variant"),
            "max_chunk_duration": util.round_time(max_chunk_duration)
            if max_chunk_duration is not None
            else None,
            "boundary_resolution": best_variant.get("boundary_trace") or [],
        }
    ]
    trace.extend(attempt_trace)
    if best_plan:
        trace.append(
            {
                "strategy": "portrait_boundary_react_planner",
                "status": "selected",
                "selected_variant": best_variant.get("variant"),
                "score": round(best_score, 3),
            }
        )
        return best_plan, trace

    trace.append(
        {
            "strategy": "portrait_boundary_react_planner",
            "status": "failed",
            "reason": "所有边界切法和候选范围都无法组成完整 portrait 主轴。",
        }
    )
    return None, trace
