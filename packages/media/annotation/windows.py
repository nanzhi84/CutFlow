"""Window planning (annotation phase 1).

Cut material into bounded analysis windows - the core defence against context
blowup: any-length material becomes several [window_min_sec, window_max_sec]
windows, each analyzed independently. Pure, deterministic, fully unit-testable;
no settings, no IO. Sensor-layer products (PySceneDetect cuts / Silero VAD
islands) are fed in as plain data.

Case A - many ultra-short shots (fast cuts). Adjacent shots merge forward to
         >= window_min_sec; a merged result over window_max_sec is split evenly.
         Normal shots <= window_max_sec become a window directly.

Case B - no cuts / single take (shot_cuts empty, or one shot > window_max_sec).
         Mechanical cuts = {w_max, 2*w_max, ...}; talking-head snaps each
         mechanical cut to the nearest voice-activity boundary within
         +/-vad_adhesion_range (island.end first, then island.start); adjacent
         snapped cuts < vad_merge_eps are deduped. B-roll without speech is
         purely mechanical, no degradation.

Either path guarantees windows contiguously cover [0, duration]: no gaps, no
overlaps, every window end > start.
"""

from __future__ import annotations

import math

from packages.core.contracts import AnalysisWindow, SpeechIslandV4, WindowReason

# Numeric tolerance for open-interval / coincident-boundary tests.
_EPS = 1e-6

# Speech-island input may be SpeechIslandV4 instances or plain {"start","end"} dicts.
IslandInput = SpeechIslandV4 | dict


def plan_windows(
    *,
    duration: float,
    shot_cuts: list[float],
    speech_islands: list[IslandInput] | None,
    window_min_sec: float = 3.0,
    window_max_sec: float = 10.0,
    vad_adhesion_range: float = 0.5,
    vad_merge_eps: float = 0.1,
) -> list[AnalysisWindow]:
    """Cut [0, duration] into bounded analysis windows.

    Args:
        duration: total material length (sec). <=0 returns [].
        shot_cuts: PySceneDetect cuts (sec, absolute). Empty = single take.
        speech_islands: Silero VAD islands; None/[] = no speech (b-roll normal path).
        window_min_sec: target lower bound; ultra-short shots merge up to this.
        window_max_sec: target upper bound; over it splits (Case A) or mechanical-cuts (Case B).
        vad_adhesion_range: VAD snap search radius (sec).
        vad_merge_eps: adjacent-boundary dedup threshold (sec).

    Returns:
        Windows contiguously covering [0, duration], ascending.
    """
    duration = float(duration or 0.0)
    if duration <= 0:
        return []

    window_max_sec = max(_EPS, float(window_max_sec))
    window_min_sec = max(0.0, float(window_min_sec))

    cuts = _sanitize_cuts(shot_cuts, duration)

    # Case B: no cuts (single take).
    if not cuts:
        if duration <= window_max_sec + _EPS:
            return [
                AnalysisWindow(
                    start=0.0, end=duration, reason=WindowReason.scene_boundary
                )
            ]
        return _plan_mechanical(
            start=0.0,
            end=duration,
            speech_islands=speech_islands,
            window_max_sec=window_max_sec,
            vad_adhesion_range=vad_adhesion_range,
            vad_merge_eps=vad_merge_eps,
        )

    # Case A: cuts present -> reconstruct shots, merge short, split long.
    scenes = _scenes_from_cuts(cuts, duration)
    merged = _merge_short_scenes(scenes, window_min_sec=window_min_sec)
    return _split_long_scenes(merged, window_max_sec=window_max_sec)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize_cuts(shot_cuts: list[float] | None, duration: float) -> list[float]:
    """Clean cuts: drop <=0 / >=duration / non-numeric, dedupe, ascending."""
    seen: list[float] = []
    for raw in shot_cuts or []:
        try:
            c = float(raw)
        except (TypeError, ValueError):
            continue
        if c <= _EPS or c >= duration - _EPS:
            continue
        seen.append(c)
    seen.sort()
    deduped: list[float] = []
    for c in seen:
        if not deduped or (c - deduped[-1]) > _EPS:
            deduped.append(c)
    return deduped


def _scenes_from_cuts(cuts: list[float], duration: float) -> list[list[float]]:
    """Reconstruct shot ranges [start, end] from cleaned cuts + duration."""
    bounds = [0.0, *cuts, duration]
    return [[bounds[i], bounds[i + 1]] for i in range(len(bounds) - 1)]


def _merge_short_scenes(
    scenes: list[list[float]],
    *,
    window_min_sec: float,
) -> list[list[float]]:
    """Case A: merge adjacent shots forward until the run reaches window_min_sec.

    A trailing run still under the lower bound (no successor) folds into its
    predecessor. Returns [start, end, merged_count] triples.
    """
    if not scenes:
        return []

    merged: list[list[float]] = []
    cur_start = scenes[0][0]
    cur_end = scenes[0][1]
    cur_count = 1

    for nxt_start, nxt_end in scenes[1:]:
        if (cur_end - cur_start) < window_min_sec - _EPS:
            cur_end = nxt_end
            cur_count += 1
        else:
            merged.append([cur_start, cur_end, cur_count])
            cur_start, cur_end, cur_count = nxt_start, nxt_end, 1

    merged.append([cur_start, cur_end, cur_count])

    if (
        len(merged) >= 2
        and (merged[-1][1] - merged[-1][0]) < window_min_sec - _EPS
    ):
        tail = merged.pop()
        merged[-1][1] = tail[1]
        merged[-1][2] += tail[2]

    return merged


def _split_long_scenes(
    merged_scenes: list[list[float]],
    *,
    window_max_sec: float,
) -> list[AnalysisWindow]:
    """Case A: turn merged shots into windows; split any over window_max_sec evenly."""
    windows: list[AnalysisWindow] = []
    for start, end, count in merged_scenes:
        span = end - start
        if span <= window_max_sec + _EPS:
            reason = (
                WindowReason.merged_short
                if count > 1
                else WindowReason.scene_boundary
            )
            windows.append(AnalysisWindow(start=start, end=end, reason=reason))
            continue
        n = max(1, math.ceil(span / window_max_sec - _EPS))
        step = span / n
        reason = (
            WindowReason.merged_short
            if count > 1
            else WindowReason.long_scene_split
        )
        for i in range(n):
            seg_start = start + i * step
            seg_end = end if i == n - 1 else start + (i + 1) * step
            windows.append(AnalysisWindow(start=seg_start, end=seg_end, reason=reason))
    return windows


def _plan_mechanical(
    *,
    start: float,
    end: float,
    speech_islands: list[IslandInput] | None,
    window_max_sec: float,
    vad_adhesion_range: float,
    vad_merge_eps: float,
) -> list[AnalysisWindow]:
    """Case B: mechanical cuts {start+w_max, ...}; talking-head snaps to VAD boundaries."""
    mechanical_cuts: list[float] = []
    n_cuts = max(0, math.ceil((end - start) / window_max_sec - _EPS) - 1)
    for i in range(1, n_cuts + 1):
        mechanical_cuts.append(start + i * window_max_sec)

    if not mechanical_cuts:
        return [AnalysisWindow(start=start, end=end, reason=WindowReason.mechanical)]

    islands = _sanitize_islands(speech_islands)
    boundaries = _collect_island_boundaries(islands)
    snapped: list[list] = []
    for mc in mechanical_cuts:
        pos, was_snapped = _snap_cut_to_vad(
            mc, boundaries, vad_adhesion_range=vad_adhesion_range
        )
        snapped.append([pos, was_snapped])

    snapped.sort(key=lambda x: x[0])
    deduped: list[list] = []
    for pos, was_snapped in snapped:
        if pos <= start + _EPS or pos >= end - _EPS:
            continue
        if deduped and (pos - deduped[-1][0]) < max(vad_merge_eps, _EPS):
            deduped[-1][1] = deduped[-1][1] or was_snapped
            continue
        deduped.append([pos, was_snapped])

    windows: list[AnalysisWindow] = []
    prev = start
    for pos, was_snapped in deduped:
        windows.append(
            AnalysisWindow(
                start=prev,
                end=pos,
                reason=WindowReason.vad_snapped
                if was_snapped
                else WindowReason.mechanical,
            )
        )
        prev = pos
    windows.append(AnalysisWindow(start=prev, end=end, reason=WindowReason.mechanical))
    return windows


def _sanitize_islands(
    speech_islands: list[IslandInput] | None,
) -> list[dict[str, float]]:
    """Clean speech islands: drop illegal (end<=start / missing fields)."""
    out: list[dict[str, float]] = []
    for isl in speech_islands or []:
        if isinstance(isl, SpeechIslandV4):
            out.append({"start": isl.start, "end": isl.end})
            continue
        try:
            s = float(isl.get("start"))
            e = float(isl.get("end"))
        except (TypeError, ValueError, AttributeError):
            continue
        if e > s + _EPS:
            out.append({"start": s, "end": e})
    return out


def _collect_island_boundaries(
    islands: list[dict[str, float]],
) -> list[dict[str, float]]:
    """Split islands into snappable boundary points tagged by kind (end / start)."""
    boundaries: list[dict[str, float]] = []
    for isl in islands:
        boundaries.append({"pos": isl["end"], "kind": "end"})
        boundaries.append({"pos": isl["start"], "kind": "start"})
    return boundaries


def _snap_cut_to_vad(
    cut: float,
    boundaries: list[dict[str, float]],
    *,
    vad_adhesion_range: float,
) -> tuple[float, bool]:
    """Snap one mechanical cut to the nearest VAD boundary within +/-vad_adhesion_range.

    "End" (after speech) beats "start" (before speech); no boundary in range keeps
    the mechanical point. Returns (final position, snapped?).
    """
    if not boundaries:
        return cut, False

    in_range = [
        b for b in boundaries if abs(b["pos"] - cut) <= vad_adhesion_range + _EPS
    ]
    if not in_range:
        return cut, False

    def _key(b: dict[str, float]):
        kind_rank = 0 if b["kind"] == "end" else 1
        return (kind_rank, abs(b["pos"] - cut), b["pos"])

    best = min(in_range, key=_key)
    return float(best["pos"]), True
