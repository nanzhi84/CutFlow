"""Clip-boundary cut-precision defences 2/3/4 (defence 1, the precise cuts, is the
shots sensor).

1. precise cuts  : PySceneDetect frame-accurate real cuts (sensors.shots, not here)
2. snap-to-cut   : snap VLM-estimated segment start/end to the nearest real cut -> snap_to_cuts
3. safety inset  : inset each end by 1-2 frames (~0.04s), deterministic fallback -> apply_safety_inset
4. internal self-check : after cutting, re-scan inside the segment for a hidden cut -> has_internal_cut

Pure, deterministic, unit-testable; no settings read (parameters are defaults).
"""

from __future__ import annotations


def snap_to_cuts(
    start: float,
    end: float,
    shot_cuts: list[float],
    *,
    tol: float = 0.25,
) -> tuple[float, float]:
    """Defence 2 - snap each endpoint to the nearest real cut within ``tol``.

    An endpoint with no cut within ``tol`` keeps its original value. Ties take the
    earlier (smaller) cut. After snapping the span must stay non-empty: if both
    ends snap to the same/reversed position, the collapsing end reverts to its
    original (start's snap is preferred).
    """
    snapped_start = _nearest_cut(start, shot_cuts, tol)
    snapped_end = _nearest_cut(end, shot_cuts, tol)

    if snapped_end <= snapped_start:
        if snapped_end != end:
            snapped_end = end
        elif snapped_start != start:
            snapped_start = start
    return snapped_start, snapped_end


def _nearest_cut(value: float, shot_cuts: list[float], tol: float) -> float:
    """Nearest cut to ``value`` within ``tol``; ``value`` itself if none. Ties take
    the earlier cut for determinism."""
    if not shot_cuts:
        return value

    best_cut: float | None = None
    best_dist: float | None = None
    for cut in sorted(shot_cuts):
        dist = abs(cut - value)
        if dist > tol:
            continue
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_cut = cut

    return best_cut if best_cut is not None else value


def apply_safety_inset(
    start: float,
    end: float,
    *,
    fps: float = 25.0,
    inset_frames: int = 1,
) -> tuple[float, float] | None:
    """Defence 3 - inset each end by ``inset_frames`` frames (= inset_frames/fps sec).

    Even with a +/-1 frame error in defence 1, a neighbour's frame can't bleed in.
    Returns None when the inset would collapse/reverse the span (too short), so the
    caller drops the segment. An already-illegal (zero/reversed) input returns None.
    """
    if end <= start:
        return None

    if inset_frames <= 0 or fps <= 0:
        return start, end

    inset = inset_frames / fps
    new_start = start + inset
    new_end = end - inset

    if new_end <= new_start:
        return None

    return new_start, new_end


def has_internal_cut(
    start: float,
    end: float,
    shot_cuts: list[float],
    *,
    edge_guard: float = 0.12,
) -> bool:
    """Defence 4 - True if a real cut sits inside (start, end) beyond the edge guard.

    The segment is bounded by its own end cuts; cuts within ``edge_guard`` of
    start/end are the segment's boundary, not intruders. A very short segment with
    overlapping edge guards (no middle) never reports.
    """
    if not shot_cuts:
        return False

    inner_low = start + edge_guard
    inner_high = end - edge_guard
    if inner_high <= inner_low:
        return False

    for cut in shot_cuts:
        if inner_low < cut < inner_high:
            return True
    return False
