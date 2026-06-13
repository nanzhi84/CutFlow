"""Candidate scopes + selected-segment construction for the capacity packer.

Ported from editing_agent/boundary_planning.py. Split out of packing.py so each file
stays cohesive: this module owns (a) the freshness candidate scopes (with their
scope_penalty + relax passes) and (b) turning a position->candidate assignment into
plan segments carrying timeline + source spans (seconds). The packer (packing.py)
owns the beam/rescue orchestration over these.
"""

from __future__ import annotations

from typing import Any

from packages.planning.editing import _candidate, _util as util


def portrait_boundary_candidate_scopes(
    portrait_candidates: list[dict[str, Any]],
    *,
    include_unlimited_reuse_scope: bool = True,
) -> list[dict[str, Any]]:
    """Freshness scopes tried in order: prefer fresh-unique, then widen reluctantly.

    Each scope carries its own scope_penalty (favours fresher scopes in the final
    score) and relax_passes (gradually allow reuse / adjacency / original-source).
    """
    all_candidates = [c for c in list(portrait_candidates or []) if isinstance(c, dict)]
    fresh_candidates = [c for c in all_candidates if not _candidate.is_recent_portrait_candidate(c)]
    scopes: list[dict[str, Any]] = []
    if fresh_candidates:
        scopes.append(
            {
                "scope": "fresh_unique",
                "candidates": fresh_candidates,
                "scope_penalty": 2000.0,
                "ignore_repetition_penalty": False,
                "relax_passes": [
                    {"allow_adjacent": False, "max_uses": 1, "allow_original": False},
                    {"allow_adjacent": False, "max_uses": 1, "allow_original": True},
                ],
            }
        )
        scopes.append(
            {
                "scope": "fresh_limited_reuse",
                "candidates": fresh_candidates,
                "scope_penalty": 1000.0,
                "ignore_repetition_penalty": False,
                "relax_passes": [
                    {"allow_adjacent": False, "max_uses": 2, "allow_original": False},
                    {"allow_adjacent": True, "max_uses": 2, "allow_original": False},
                    {"allow_adjacent": True, "max_uses": 2, "allow_original": True},
                ],
            }
        )
    scopes.append(
        {
            "scope": "all_penalty",
            "candidates": all_candidates,
            "scope_penalty": 0.0,
            "ignore_repetition_penalty": False,
            "relax_passes": [
                {"allow_adjacent": False, "max_uses": 2, "allow_original": False},
                {"allow_adjacent": True, "max_uses": 2, "allow_original": False},
                {"allow_adjacent": True, "max_uses": 2, "allow_original": True},
            ],
        }
    )
    if include_unlimited_reuse_scope:
        scopes.append(
            {
                "scope": "capacity_fallback_all",
                "candidates": all_candidates,
                "scope_penalty": -1000.0,
                "ignore_repetition_penalty": True,
                "relax_passes": [
                    {"allow_adjacent": True, "max_uses": 999, "allow_original": False},
                    {"allow_adjacent": True, "max_uses": 999, "allow_original": True},
                ],
            }
        )
    return scopes


def build_boundary_selected_segment(
    *,
    best_choice: dict[str, Any],
    chunk: dict[str, Any],
    slot_index: int,
    pause_windows: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    duration = util.round_time(chunk["duration"])
    template_id = str(best_choice.get("template_id") or "").strip()
    window_id = str(best_choice.get("window_id") or "").strip()
    source_window_start = util.round_time(
        best_choice.get("_source_window_start", best_choice.get("start", 0.0))
    )
    source_window_end = util.round_time(
        best_choice.get("_source_window_end", best_choice.get("end", 0.0))
    )
    source_start = util.round_time(best_choice.get("_assigned_source_start", best_choice.get("start", 0.0)))
    source_end = util.round_time(source_start + duration)
    if source_window_end <= source_window_start:
        source_window_end = util.round_time(source_start + duration)
    return {
        "index": slot_index + 1,
        "segment_type": "portrait",
        "timeline_start": util.round_time(chunk["start"]),
        "timeline_end": util.round_time(chunk["end"]),
        "start": source_start,
        "end": source_end,
        "source_start": source_start,
        "source_end": source_end,
        "source_window_start": source_window_start,
        "source_window_end": source_window_end,
        "source_window_duration": util.round_time(best_choice.get("duration", duration)),
        "duration": duration,
        "template_id": template_id,
        "template_name": best_choice.get("template_name"),
        "role": best_choice.get("role"),
        "slot_phase": chunk.get("phase"),
        "slot_index": slot_index,
        "source_mode": _candidate.candidate_source_mode(best_choice),
        "reason": str(best_choice.get("reason") or "按气口/句尾边界选择可覆盖完整区间的人像素材").strip(),
        "selected_candidate_id": window_id,
        "original_window_id": best_choice.get("original_window_id"),
        "diversity_key": best_choice.get("diversity_key"),
        "source_window_reuse_index": int(best_choice.get("_source_window_reuse_index", 0) or 0),
        "freshness_scope": best_choice.get("_freshness_scope"),
        "recently_used_material": _candidate.is_recent_portrait_candidate(best_choice),
        "material_reuse_fallback": bool(
            best_choice.get("material_reuse_fallback") or chunk.get("material_reuse_fallback")
        ),
        "reuse_policy": best_choice.get("reuse_policy") or chunk.get("reuse_policy"),
        "boundary_policy": "portrait_semantic_audio_pause_capacity_locked"
        if pause_windows
        else "portrait_sentence_end_capacity_locked",
        "unit_ids": list(chunk.get("unit_ids") or []),
        "semantic_start": chunk.get("semantic_start"),
        "semantic_end": chunk.get("semantic_end"),
        "boundary_source": chunk.get("boundary_source"),
        "boundary_reason": chunk.get("boundary_reason"),
        "boundary_unit_id": chunk.get("boundary_unit_id"),
        "pause_window_start": chunk.get("pause_window_start"),
        "pause_window_end": chunk.get("pause_window_end"),
        "pause_duration_ms": chunk.get("pause_duration_ms", 0),
    }


def selected_segments_from_assignment(
    *,
    chunks: list[dict[str, Any]],
    assignment: dict[int, dict[str, Any]],
    candidate_scope: str,
    pause_windows: list[dict[str, float]] | None,
) -> list[dict[str, Any]]:
    """Turn a position->candidate assignment (beam or rescue) into plan segments.

    When a window is reused across slots, the source start advances by the prior
    slots' durations (so reuse cuts a fresh sub-span, not the same frames twice).
    """
    selected: list[dict[str, Any]] = []
    selected_window_offsets: dict[str, float] = {}
    selected_window_counts: dict[str, int] = {}
    for idx in range(len(chunks)):
        candidate = dict(assignment[idx])
        window_id = str(candidate.get("window_id") or "").strip()
        base_source_start = util.round_time(candidate.get("start", 0.0))
        source_offset = util.round_time(selected_window_offsets.get(window_id, 0.0))
        candidate["_assigned_source_start"] = util.round_time(base_source_start + source_offset)
        candidate["_source_window_start"] = base_source_start
        candidate["_source_window_end"] = util.round_time(
            candidate.get("end", base_source_start + candidate.get("duration", 0.0))
        )
        candidate["_source_window_reuse_index"] = selected_window_counts.get(window_id, 0)
        candidate["_freshness_scope"] = candidate_scope
        selected.append(
            build_boundary_selected_segment(
                best_choice=candidate,
                chunk=chunks[idx],
                slot_index=idx,
                pause_windows=pause_windows,
            )
        )
        selected_window_offsets[window_id] = util.round_time(
            source_offset + util.round_time(chunks[idx].get("duration", 0.0))
        )
        selected_window_counts[window_id] = selected_window_counts.get(window_id, 0) + 1
    return selected
