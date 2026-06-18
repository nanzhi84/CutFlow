"""Semantic + audio-pause boundary assembly (ported from editing_agent/semantic_audio.py).

Pure functions: read narration units (+ optional pause windows) + target duration and
emit ordered boundary entries. When pause windows are given, semantic sentence-ends
are only kept if a strong real pause sits nearby (the cut snaps into the silence);
when no pause windows are given the planner falls back to SEMANTIC-ONLY boundaries
(every eligible sentence end is a boundary). Long gaps between accepted boundaries
get a semantic (or capacity audio-pause) fallback boundary injected so no single
portrait chunk runs longer than the gap cap.
"""

from __future__ import annotations

from typing import Any

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.editing import _util as util
from packages.planning.editing import audio_pause
from packages.planning.editing.constants import (
    AUDIO_PAUSE_CUT_OFFSET,
    AUDIO_PAUSE_STRONG_MIN_DURATION,
    BOUNDARY_LONG_GAP_HARD_MAX_DURATION,
    BOUNDARY_LONG_GAP_MIN_SEGMENT,
)


def build_semantic_audio_boundary_entries(
    narration_units: list[NarrationUnit],
    target_duration: float,
    pause_windows: list[dict[str, float]] | None = None,
    max_gap_duration: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target_ceiling = util.round_time(target_duration)
    if target_ceiling <= 0.08:
        return [], []

    use_audio_pauses = bool(pause_windows)
    entries: list[dict[str, Any]] = [
        {
            "boundary": 0.0,
            "semantic_boundary": 0.0,
            "boundary_source": "timeline_start",
            "unit_id": None,
            "reason": "时间线开始",
        },
        {
            "boundary": target_ceiling,
            "semantic_boundary": target_ceiling,
            "boundary_source": "timeline_end",
            "unit_id": None,
            "reason": "时间线结尾",
        },
    ]
    trace: list[dict[str, Any]] = [
        {
            "strategy": "semantic_audio_boundary_filter",
            "status": "started",
            "use_audio_pause_windows": use_audio_pauses,
            "pause_window_count": len(pause_windows or []),
        }
    ]
    semantic_candidates: list[dict[str, Any]] = []

    for unit in narration_units or []:
        primary_boundary = bool(unit.portrait_cut_allowed or unit.hard_end)
        capacity_boundary = _is_capacity_boundary_candidate(unit)
        if not (primary_boundary or capacity_boundary):
            continue
        semantic_boundary = util.round_time(util.clamp(unit.end, 0.0, target_ceiling))
        if not (0.08 < semantic_boundary < target_ceiling - 0.08):
            continue

        entry: dict[str, Any] = {
            "semantic_boundary": semantic_boundary,
            "unit_id": unit.unit_id,
            "reason": unit.boundary_reason,
            "boundary_score": unit.boundary_score,
            "pause_after_ms": unit.pause_after_ms,
        }
        semantic_entry = _boundary_entry(entry, boundary=semantic_boundary, source="semantic_only")
        if use_audio_pauses:
            pause_entry: dict[str, Any] | None = None
            matched_pause = audio_pause.match_audio_pause_window(
                semantic_boundary,
                pause_windows,
                min_duration=AUDIO_PAUSE_STRONG_MIN_DURATION,
                allow_delay=True,
                allow_advance=False,
            )
            if matched_pause:
                resolved_boundary = util.round_time(
                    util.clamp(matched_pause["cut_point"], 0.0, target_ceiling)
                )
                if 0.08 < resolved_boundary < target_ceiling - 0.08:
                    pause_entry = _boundary_entry(
                        entry,
                        boundary=resolved_boundary,
                        source="semantic_audio_pause",
                        pause=matched_pause,
                    )
            if primary_boundary:
                semantic_candidates.append(pause_entry or semantic_entry)
            elif pause_entry:
                semantic_candidates.append(pause_entry)
            if not primary_boundary:
                continue
            if pause_entry is None:
                trace.append(
                    {
                        "strategy": "semantic_audio_boundary_filter",
                        "status": "skipped",
                        "unit_id": unit.unit_id,
                        "semantic_boundary": semantic_boundary,
                        "reason": "语义句尾附近没有可靠真实气口，边界降级并合并到后续句子。",
                    }
                )
                continue
            resolved_boundary = util.round_time(pause_entry["boundary"])
            entry.update(pause_entry)
            trace.append(
                {
                    "strategy": "semantic_audio_boundary_filter",
                    "status": "accepted",
                    "unit_id": unit.unit_id,
                    "semantic_boundary": semantic_boundary,
                    "applied_boundary": resolved_boundary,
                    "delta": util.round_time(resolved_boundary - semantic_boundary),
                    "reason": f"{unit.boundary_reason} + 真实音频气口",
                }
            )
        else:
            semantic_candidates.append(semantic_entry)
            if not primary_boundary:
                continue
            entry.update(semantic_entry)
        entries.append(entry)

    entries = sorted(entries, key=lambda item: float(item.get("boundary", 0.0)))
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        boundary = util.round_time(entry.get("boundary", 0.0))
        if not deduped:
            deduped.append({**entry, "boundary": boundary})
            continue
        previous = deduped[-1]
        if boundary <= util.round_time(previous.get("boundary", 0.0)) + 0.08:
            previous_score = util.as_float(previous.get("boundary_score"), 0.0)
            current_score = util.as_float(entry.get("boundary_score"), 0.0)
            if current_score > previous_score and str(previous.get("boundary_source")) not in {
                "timeline_start",
                "timeline_end",
            }:
                deduped[-1] = {**entry, "boundary": boundary}
            continue
        deduped.append({**entry, "boundary": boundary})
    if use_audio_pauses or max_gap_duration is not None:
        deduped, long_gap_trace = inject_semantic_fallbacks_for_long_gaps(
            boundary_entries=deduped,
            semantic_candidates=semantic_candidates,
            pause_windows=pause_windows,
            max_gap_duration=max_gap_duration,
        )
        trace.extend(long_gap_trace)
    return deduped, trace


def _is_capacity_boundary_candidate(unit: NarrationUnit) -> bool:
    if unit.portrait_cut_allowed or unit.hard_end:
        return True
    reason = str(unit.boundary_reason or "").strip()
    return bool(reason) and unit.boundary_score > 0


def _boundary_entry(
    base: dict[str, Any],
    *,
    boundary: float,
    source: str,
    pause: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **base,
        "boundary": boundary,
        "boundary_source": source,
        "pause_window_start": pause.get("start") if pause else None,
        "pause_window_end": pause.get("end") if pause else None,
        "pause_duration_ms": (
            int(round(util.as_float(pause.get("duration"), 0.0) * 1000)) if pause else 0
        ),
        "distance_to_boundary": pause.get("distance_to_boundary") if pause else None,
    }


def select_semantic_fallback_for_long_gap(
    *,
    gap_start: float,
    gap_end: float,
    semantic_candidates: list[dict[str, Any]],
    existing_boundaries: set[float],
    max_gap_duration: float | None = None,
) -> dict[str, Any] | None:
    min_segment = _capacity_min_segment(max_gap_duration)
    lower = util.round_time(gap_start + min_segment)
    upper = util.round_time(gap_end - min_segment)
    if upper <= lower:
        return None

    eligible = [
        dict(candidate)
        for candidate in semantic_candidates
        if isinstance(candidate, dict)
        and util.round_time(candidate.get("boundary", 0.0)) not in existing_boundaries
        and lower < util.round_time(candidate.get("boundary", 0.0)) < upper
    ]
    if not eligible:
        return None

    effective_max_gap = util.round_time(max_gap_duration or BOUNDARY_LONG_GAP_HARD_MAX_DURATION)
    hard_limit = util.round_time(gap_start + effective_max_gap)
    within_limit = [
        candidate
        for candidate in eligible
        if util.round_time(candidate.get("boundary", 0.0)) <= hard_limit + 1e-6
    ]
    if within_limit:
        return max(
            within_limit,
            key=lambda candidate: (
                util.round_time(candidate.get("boundary", 0.0)),
                util.as_float(candidate.get("boundary_score"), 0.0),
            ),
        )

    return min(
        eligible,
        key=lambda candidate: (
            util.round_time(candidate.get("boundary", 0.0)),
            -util.as_float(candidate.get("boundary_score"), 0.0),
        ),
    )


def select_audio_pause_fallback_for_long_gap(
    *,
    gap_start: float,
    gap_end: float,
    pause_windows: list[dict[str, float]] | None,
    existing_boundaries: set[float],
    max_gap_duration: float,
) -> dict[str, Any] | None:
    min_segment = _capacity_min_segment(max_gap_duration)
    lower = util.round_time(gap_start + min_segment)
    upper = util.round_time(gap_end - min_segment)
    if upper <= lower:
        return None

    effective_max_gap = util.round_time(max_gap_duration)
    hard_limit = util.round_time(gap_start + effective_max_gap)
    candidates: list[dict[str, Any]] = []
    for window in pause_windows or []:
        start = util.round_time(window.get("start", 0.0))
        end = util.round_time(window.get("end", start))
        duration = util.as_float(window.get("duration"), max(0.0, end - start))
        if duration + 1e-6 < AUDIO_PAUSE_STRONG_MIN_DURATION:
            continue
        cut_point = util.round_time(util.clamp(start + AUDIO_PAUSE_CUT_OFFSET, start, end))
        if cut_point in existing_boundaries:
            continue
        if not (lower < cut_point < upper):
            continue
        candidates.append(
            {
                "boundary": cut_point,
                "semantic_boundary": cut_point,
                "boundary_source": "audio_pause_capacity_fallback",
                "unit_id": None,
                "reason": "真实音频气口",
                "boundary_score": 0.62 + min(max(duration, 0.0) / 1.0, 0.25),
                "pause_after_ms": int(round(duration * 1000)),
                "pause_window_start": start,
                "pause_window_end": end,
                "pause_duration_ms": int(round(duration * 1000)),
                "distance_to_boundary": 0.0,
            }
        )
    if not candidates:
        return None

    within_limit = [
        candidate
        for candidate in candidates
        if util.round_time(candidate.get("boundary", 0.0)) <= hard_limit + 1e-6
    ]
    if within_limit:
        return max(
            within_limit,
            key=lambda candidate: (
                util.round_time(candidate.get("boundary", 0.0)),
                util.as_float(candidate.get("pause_duration_ms"), 0.0),
            ),
        )

    # 0.01s margin family (prod 854b5244): cut = window_start + offset can land just
    # past hard_limit, but the silence has width — when window_start is still <=
    # hard_limit, nudge the cut back to a compliant in-window position instead of
    # picking an out-of-bounds candidate.
    nudged: list[dict[str, Any]] = []
    for candidate in candidates:
        window_start = util.round_time(util.as_float(candidate.get("pause_window_start"), 0.0))
        window_end = util.round_time(util.as_float(candidate.get("pause_window_end"), window_start))
        if window_start > hard_limit + 1e-6:
            continue
        nudged_cut = util.round_time(min(hard_limit, window_end - AUDIO_PAUSE_CUT_OFFSET))
        if nudged_cut < window_start - 1e-6:
            continue
        if nudged_cut in existing_boundaries:
            continue
        if not (lower < nudged_cut < upper):
            continue
        nudged.append(
            {
                **candidate,
                "boundary": nudged_cut,
                "semantic_boundary": nudged_cut,
                "cut_nudged_into_pause": True,
            }
        )
    if nudged:
        return max(
            nudged,
            key=lambda candidate: (
                util.round_time(candidate.get("boundary", 0.0)),
                util.as_float(candidate.get("pause_duration_ms"), 0.0),
            ),
        )

    return min(
        candidates,
        key=lambda candidate: (
            util.round_time(candidate.get("boundary", 0.0)),
            -util.as_float(candidate.get("pause_duration_ms"), 0.0),
        ),
    )


def _capacity_min_segment(max_gap_duration: float | None) -> float:
    if max_gap_duration is None:
        return BOUNDARY_LONG_GAP_MIN_SEGMENT
    effective_max_gap = util.round_time(max_gap_duration)
    return min(BOUNDARY_LONG_GAP_MIN_SEGMENT, max(1.5, effective_max_gap * 0.35))


def inject_semantic_fallbacks_for_long_gaps(
    *,
    boundary_entries: list[dict[str, Any]],
    semantic_candidates: list[dict[str, Any]],
    pause_windows: list[dict[str, float]] | None = None,
    max_gap_duration: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(boundary_entries) <= 1:
        return boundary_entries, []

    effective_max_gap = util.round_time(max_gap_duration or BOUNDARY_LONG_GAP_HARD_MAX_DURATION)
    capacity_driven = (
        max_gap_duration is not None
        and effective_max_gap < util.round_time(BOUNDARY_LONG_GAP_HARD_MAX_DURATION) - 1e-6
    )
    if not semantic_candidates and not (capacity_driven and pause_windows):
        return boundary_entries, []

    existing_boundaries = {
        util.round_time(entry.get("boundary", 0.0)) for entry in boundary_entries
    }
    protected: list[dict[str, Any]] = [dict(boundary_entries[0])]
    trace: list[dict[str, Any]] = []

    for next_entry in boundary_entries[1:]:
        current_boundary = util.round_time(protected[-1].get("boundary", 0.0))
        next_boundary = util.round_time(next_entry.get("boundary", 0.0))
        while next_boundary - current_boundary > effective_max_gap + 1e-6:
            fallback = select_semantic_fallback_for_long_gap(
                gap_start=current_boundary,
                gap_end=next_boundary,
                semantic_candidates=semantic_candidates,
                existing_boundaries=existing_boundaries,
                max_gap_duration=effective_max_gap,
            )
            fallback_source = "semantic"
            semantic_fits_capacity = bool(
                fallback
                and util.round_time(fallback.get("boundary", 0.0))
                <= current_boundary + effective_max_gap + 1e-6
            )
            if capacity_driven and not semantic_fits_capacity:
                fallback = select_audio_pause_fallback_for_long_gap(
                    gap_start=current_boundary,
                    gap_end=next_boundary,
                    pause_windows=pause_windows,
                    existing_boundaries=existing_boundaries,
                    max_gap_duration=effective_max_gap,
                )
                fallback_source = "audio_pause" if fallback else fallback_source
            if not fallback:
                trace.append(
                    {
                        "strategy": "semantic_audio_boundary_filter",
                        "status": "capacity_gap_unprotected" if capacity_driven else "long_gap_unprotected",
                        "gap_start": current_boundary,
                        "gap_end": next_boundary,
                        "gap_duration": util.round_time(next_boundary - current_boundary),
                        "max_gap_duration": effective_max_gap,
                    }
                )
                break

            injected_boundary = util.round_time(fallback.get("boundary", 0.0))
            injected = {
                **fallback,
                "boundary": injected_boundary,
                "boundary_source": (
                    "audio_pause_capacity_fallback"
                    if capacity_driven and fallback_source == "audio_pause"
                    else ("semantic_capacity_fallback" if capacity_driven else "semantic_long_gap_fallback")
                ),
                "reason": (
                    f"{str(fallback.get('reason') or '脚本句尾').strip()} + 素材容量拆分"
                    if capacity_driven
                    else f"{str(fallback.get('reason') or '脚本句尾').strip()} + 超长区间保护"
                ),
            }
            protected.append(injected)
            existing_boundaries.add(injected_boundary)
            trace.append(
                {
                    "strategy": "semantic_audio_boundary_filter",
                    "status": (
                        "injected_audio_pause_capacity_fallback"
                        if capacity_driven and fallback_source == "audio_pause"
                        else ("injected_capacity_fallback" if capacity_driven else "injected_long_gap_fallback")
                    ),
                    "gap_start": current_boundary,
                    "gap_end": next_boundary,
                    "gap_duration": util.round_time(next_boundary - current_boundary),
                    "applied_boundary": injected_boundary,
                    "unit_id": injected.get("unit_id"),
                    "max_gap_duration": effective_max_gap,
                }
            )
            current_boundary = injected_boundary

        protected.append(dict(next_entry))

    return protected, trace
