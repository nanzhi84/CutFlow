"""Boundary-locked chunk building + inventory-aware capacity variants.

Ported from editing_agent/boundary_planning.py (the chunk half). Turns the ordered
boundary entries into portrait chunks that ONLY end on a narration boundary (or the
final tail), with the origin's rhythm-cost calibration (preferred / min / max chunk
durations). Capacity variants derive tighter chunk caps from the candidate-window
duration distribution so more (smaller) source windows can participate in packing.
"""

from __future__ import annotations

import math
from typing import Any

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.editing import _util as util
from packages.planning.editing import boundary as boundary_mod
from packages.planning.editing.constants import (
    CAPACITY_CAP_MARGIN,
    CAPACITY_CAP_MAX_COUNT,
    CAPACITY_CAP_MIN_DURATION,
    CAPACITY_CAP_MIN_GAP,
)


def boundary_chunk_signature(chunks: list[dict[str, Any]]) -> tuple[tuple[float, float], ...]:
    return tuple(
        (util.round_time(chunk.get("start", 0.0)), util.round_time(chunk.get("end", 0.0)))
        for chunk in chunks or []
    )


def build_boundary_locked_chunks(
    narration_units: list[NarrationUnit],
    target_duration: float,
    pause_windows: list[dict[str, float]] | None = None,
    *,
    merge_short_tail: bool = True,
    max_chunk_duration: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build portrait chunks that only end on narration boundaries or the final tail."""
    target_ceiling = util.round_time(target_duration)
    ordered, boundary_trace = boundary_mod.build_semantic_audio_boundary_entries(
        narration_units,
        target_duration,
        pause_windows=pause_windows,
        max_gap_duration=max_chunk_duration,
    )
    if len(ordered) <= 1:
        return [], boundary_trace

    chunks: list[dict[str, Any]] = []
    index = 0
    while index < len(ordered) - 1:
        start_entry = ordered[index]
        start = util.round_time(start_entry.get("boundary", 0.0))
        is_opening = not chunks
        preferred = 7.5 if not is_opening else 6.5
        min_duration = 4.0 if is_opening else 6.0
        max_duration = 9.0
        best_boundary_index: int | None = None
        best_cost = float("inf")

        for candidate_index in range(index + 1, len(ordered)):
            end = util.round_time(ordered[candidate_index].get("boundary", 0.0))
            duration = util.round_time(end - start)
            if duration <= 0.08:
                continue
            is_last = candidate_index == len(ordered) - 1
            allowed_max = 10.0 if is_last else max_duration
            if max_chunk_duration is not None:
                allowed_max = min(allowed_max, util.round_time(max_chunk_duration))
            if duration > allowed_max + 1e-6 and best_boundary_index is not None:
                break
            if duration > allowed_max + 1e-6 and best_boundary_index is None:
                best_boundary_index = candidate_index
                break
            cost = abs(duration - preferred)
            if not is_last and duration < min_duration:
                cost += (min_duration - duration) * 1.8
            remaining = util.round_time(target_ceiling - end)
            if not is_last and 0.08 < remaining < 3.0:
                cost += 3.0
            if cost < best_cost:
                best_cost = cost
                best_boundary_index = candidate_index

        if best_boundary_index is None:
            best_boundary_index = index + 1

        end_entry = ordered[best_boundary_index]
        end = util.round_time(end_entry.get("boundary", 0.0))
        semantic_start = util.round_time(start_entry.get("semantic_boundary", start))
        semantic_end = util.round_time(end_entry.get("semantic_boundary", end))
        chunks.append(
            {
                "index": len(chunks) + 1,
                "start": start,
                "end": end,
                "duration": util.round_time(end - start),
                "phase": "opening"
                if not chunks
                else ("tail" if best_boundary_index == len(ordered) - 1 else "main"),
                "unit_ids": util.map_unit_ids_to_range(narration_units, semantic_start, semantic_end),
                "semantic_start": semantic_start,
                "semantic_end": semantic_end,
                "boundary_source": end_entry.get("boundary_source"),
                "boundary_reason": end_entry.get("reason"),
                "boundary_unit_id": end_entry.get("unit_id"),
                "pause_window_start": end_entry.get("pause_window_start"),
                "pause_window_end": end_entry.get("pause_window_end"),
                "pause_duration_ms": end_entry.get("pause_duration_ms", 0),
            }
        )
        index = best_boundary_index

    if merge_short_tail and len(chunks) > 1 and chunks[-1]["duration"] < 3.0:
        tail = chunks.pop()
        merged_duration = util.round_time(tail["end"] - chunks[-1]["start"])
        if max_chunk_duration is not None and merged_duration > util.round_time(max_chunk_duration) + 1e-6:
            chunks.append(tail)
        else:
            chunks[-1]["end"] = tail["end"]
            chunks[-1]["duration"] = util.round_time(chunks[-1]["end"] - chunks[-1]["start"])
            chunks[-1]["phase"] = "tail"
            chunks[-1]["unit_ids"] = list(
                dict.fromkeys([*(chunks[-1].get("unit_ids") or []), *(tail.get("unit_ids") or [])])
            )
            chunks[-1]["semantic_end"] = tail.get("semantic_end")
            chunks[-1]["boundary_source"] = tail.get("boundary_source")
            chunks[-1]["boundary_reason"] = tail.get("boundary_reason")
            chunks[-1]["boundary_unit_id"] = tail.get("boundary_unit_id")
            chunks[-1]["pause_window_start"] = tail.get("pause_window_start")
            chunks[-1]["pause_window_end"] = tail.get("pause_window_end")
            chunks[-1]["pause_duration_ms"] = tail.get("pause_duration_ms", 0)

    for idx, chunk in enumerate(chunks, start=1):
        chunk["index"] = idx
        if idx == 1:
            chunk["phase"] = "opening"
        elif idx == len(chunks):
            chunk["phase"] = "tail"
    return chunks, boundary_trace


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Hand-written linear-interpolation percentile (input must be ascending, non-empty)."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * util.clamp(fraction, 0.0, 1.0)
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    return sorted_values[lower_index] * (1.0 - weight) + sorted_values[upper_index] * weight


def derive_capacity_cap_durations(
    portrait_candidates: list[dict[str, Any]],
    *,
    max_chunk_duration: float,
) -> list[float]:
    """Derive inventory-aware chunk caps from the candidate-window duration distribution.

    Takes (a) the second-largest distinct duration, (b) the 70th percentile, (c) the
    50th percentile; keeps caps that are >= chunk min-duration, <= max-margin, and at
    least min-gap apart. Descending, at most CAPACITY_CAP_MAX_COUNT.
    """
    durations = sorted(
        util.round_time(util.as_float(candidate.get("duration"), 0.0))
        for candidate in list(portrait_candidates or [])
        if isinstance(candidate, dict) and util.as_float(candidate.get("duration"), 0.0) > 0.08
    )
    if not durations:
        return []
    raw_caps: list[float] = []
    unique_desc = sorted(set(durations), reverse=True)
    if len(unique_desc) >= 2:
        raw_caps.append(unique_desc[1])
    raw_caps.append(_percentile(durations, 0.7))
    raw_caps.append(_percentile(durations, 0.5))

    upper_bound = util.round_time(max_chunk_duration - CAPACITY_CAP_MARGIN)
    caps: list[float] = []
    for raw_cap in sorted({util.round_time(cap) for cap in raw_caps}, reverse=True):
        if raw_cap < CAPACITY_CAP_MIN_DURATION - 1e-6:
            continue
        if raw_cap > upper_bound + 1e-6:
            continue
        if any(abs(raw_cap - kept) < CAPACITY_CAP_MIN_GAP for kept in caps):
            continue
        caps.append(raw_cap)
        if len(caps) >= CAPACITY_CAP_MAX_COUNT:
            break
    return caps


def boundary_chunk_variants(
    *,
    narration_units: list[NarrationUnit],
    target_duration: float,
    pause_windows: list[dict[str, float]] | None = None,
    max_chunk_duration: float | None = None,
    cap_durations: list[float] | None = None,
) -> list[dict[str, Any]]:
    preferred_chunks, preferred_trace = build_boundary_locked_chunks(
        narration_units,
        target_duration,
        pause_windows=pause_windows,
        merge_short_tail=True,
        max_chunk_duration=max_chunk_duration,
    )
    variants: list[dict[str, Any]] = [
        {
            "variant": "rhythm_preferred",
            "chunks": preferred_chunks,
            "boundary_trace": preferred_trace,
            "variant_penalty": 0.0,
        }
    ]

    preserved_chunks, preserved_trace = build_boundary_locked_chunks(
        narration_units,
        target_duration,
        pause_windows=pause_windows,
        merge_short_tail=False,
        max_chunk_duration=max_chunk_duration,
    )
    if boundary_chunk_signature(preserved_chunks) != boundary_chunk_signature(preferred_chunks):
        variants.append(
            {
                "variant": "preserve_short_tail",
                "chunks": preserved_chunks,
                "boundary_trace": preserved_trace,
                "variant_penalty": -1.0,
            }
        )

    seen_signatures = {
        boundary_chunk_signature(list(variant.get("chunks") or [])) for variant in variants
    }
    ordered_caps = [
        cap
        for cap in sorted(
            {util.round_time(util.as_float(cap, 0.0)) for cap in (cap_durations or [])}, reverse=True
        )
        if cap > 0.08
    ]
    for cap_index, cap in enumerate(ordered_caps):
        cap_chunks, cap_trace = build_boundary_locked_chunks(
            narration_units,
            target_duration,
            pause_windows=pause_windows,
            merge_short_tail=True,
            max_chunk_duration=cap,
        )
        if not cap_chunks:
            continue
        if max(util.as_float(chunk.get("duration"), 0.0) for chunk in cap_chunks) > cap + 1e-6:
            # This cap can't honour its own promise (some chunk can't split below cap)
            # — it would still demand a window longer than cap; drop it.
            continue
        signature = boundary_chunk_signature(cap_chunks)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        variants.append(
            {
                "variant": f"capacity_cap_{cap:.2f}",
                "cap_duration": cap,
                "chunks": cap_chunks,
                "boundary_trace": cap_trace,
                "variant_penalty": -2.0 - float(cap_index),
                "variant_tier": cap_index + 1,
            }
        )
    return variants
