"""Audio-pause window matching (pure) — ported from editing_agent/audio_pause.py.

This is NOT pause *detection* (no ffmpeg / silencedetect — that is step 2b and out of
scope). These helpers only MATCH already-supplied pause windows to a semantic
boundary so the boundary planner can prefer cutting inside a real silence. A pause
window is a dict ``{start, end, duration, center?}``. When no pause windows are
given, the boundary planner falls back to semantic-only boundaries (these helpers
are simply not consulted).
"""

from __future__ import annotations

from typing import Any

from packages.planning.editing import _util as util
from packages.planning.editing.constants import (
    AUDIO_BOUNDARY_ADVANCE_LIMIT,
    AUDIO_PAUSE_BOUNDARY_EPS,
    AUDIO_PAUSE_CUT_OFFSET,
    AUDIO_PAUSE_SEARCH_BACK,
    AUDIO_PAUSE_SEARCH_FORWARD,
    AUDIO_PAUSE_STRONG_MIN_DURATION,
)


def match_audio_pause_window(
    boundary: float,
    pause_windows: list[dict[str, float]] | None,
    *,
    min_duration: float | None = None,
    allow_delay: bool = True,
    allow_advance: bool = True,
) -> dict[str, float] | None:
    windows = list(pause_windows or [])
    if not windows:
        return None
    effective_min_duration = (
        AUDIO_PAUSE_STRONG_MIN_DURATION if min_duration is None else max(0.0, float(min_duration))
    )
    search_forward = AUDIO_PAUSE_SEARCH_FORWARD if allow_delay else AUDIO_PAUSE_BOUNDARY_EPS
    candidates: list[dict[str, float]] = []
    for window in windows:
        start = util.as_float(window.get("start"), boundary)
        end = util.as_float(window.get("end"), start)
        duration = util.as_float(window.get("duration"), max(0.0, end - start))
        if (
            duration + 1e-6 >= effective_min_duration
            and end >= boundary - AUDIO_PAUSE_SEARCH_BACK
            and start <= boundary + search_forward
        ):
            candidates.append(
                {
                    **window,
                    "start": util.round_time(start),
                    "end": util.round_time(end),
                    "duration": util.round_time(duration),
                }
            )
    if not candidates:
        return None

    def _pause_distance(window: dict[str, float]) -> float:
        start = float(window["start"])
        end = float(window["end"])
        if start <= boundary <= end:
            return 0.0
        if start > boundary:
            return start - boundary
        return boundary - end

    if allow_advance:
        best = min(
            candidates,
            key=lambda window: (
                _pause_distance(window),
                max(0.0, window["start"] - boundary),
                -window["duration"],
            ),
        )
    else:
        best = min(
            candidates,
            key=lambda window: (
                0 if window["end"] >= boundary else 1,
                0 if window["start"] >= boundary else 1,
                _pause_distance(window),
                max(0.0, window["start"] - boundary),
                -window["duration"],
            ),
        )
    if best["start"] > boundary:
        pause_cut = util.round_time(best["start"] + AUDIO_PAUSE_CUT_OFFSET)
    elif best["end"] < boundary:
        pause_cut = util.round_time(
            max(best["start"] + AUDIO_PAUSE_CUT_OFFSET, best["end"] - AUDIO_PAUSE_CUT_OFFSET)
        )
    else:
        pause_cut = util.round_time(max(boundary, best["start"] + AUDIO_PAUSE_CUT_OFFSET))
    earliest_cut = (
        util.round_time(max(0.0, boundary - AUDIO_BOUNDARY_ADVANCE_LIMIT))
        if allow_advance
        else util.round_time(boundary)
    )
    latest_cut = util.round_time(boundary + (AUDIO_PAUSE_SEARCH_FORWARD if allow_delay else 0.0))
    return {
        **best,
        "cut_point": util.round_time(util.clamp(pause_cut, earliest_cut, latest_cut)),
        "semantic_boundary": util.round_time(boundary),
        "distance_to_boundary": util.round_time(_pause_distance(best)),
        "min_duration_required": util.round_time(effective_min_duration),
    }


def audio_pause_windows_to_payload(
    pause_windows: list[dict[str, float]] | None,
    *,
    limit: int = 120,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for window in list(pause_windows or [])[: max(0, int(limit))]:
        duration = util.as_float(window.get("duration"), 0.0)
        payload.append(
            {
                "start": util.round_time(window.get("start", 0.0)),
                "end": util.round_time(window.get("end", 0.0)),
                "duration_ms": int(round(duration * 1000)),
                "center": util.round_time(window.get("center", 0.0)),
                "strong": duration + 1e-6 >= AUDIO_PAUSE_STRONG_MIN_DURATION,
            }
        )
    return payload
