"""Real b-roll insertion planning: place ranked clips inside narration windows.

Replaces the seeded ``start_sec = index * 3`` placement. Each chosen candidate
is anchored to the narration beat it best matched (so the insert lands inside a
real spoken window, not a mechanical 0/3/6 grid), with non-overlapping timeline
windows and the source trim taken from the matched clip. Pure + deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class BrollGeometryPolicy:
    fps: int = 30
    min_insert_seconds: float = 1.5
    max_insert_seconds: float = 4.0
    full_coverage_max_segment_seconds: float = 5.5
    min_visible_aroll_seconds: float = 2.0
    snap_max_frames: int = 15
    max_pad_seconds: float = 0.15


BROLL_GEOMETRY_POLICY = BrollGeometryPolicy()

# Portrait-cut frame-grid alignment constants (#105). Moved here from the old
# downstream production helper ``_timeline_grid.align_broll_to_portrait_cuts`` so the
# frame-grid constraint is enforced at plan time, not patched after the fact:
#  - SNAP_MAX_FRAMES: largest portrait sliver (in frames) a b-roll boundary may snap
#    across to land on the cut. The pad cap below is also enforced, so the effective
#    snap window is the stricter of the frame window and the seconds cap;
#  - MIN_VISIBLE_AROLL_SECONDS: a portrait sliver shorter than this is "too short to
#    read" -> snap it away; longer -> leave the real portrait visible;
#  - MAX_PAD_SECONDS: the snap may extend the timeline window by at most this much of
#    clone-pad (the source window is never pulled past its clean span).
BROLL_PORTRAIT_CUT_SNAP_MAX_FRAMES = BROLL_GEOMETRY_POLICY.snap_max_frames
BROLL_MIN_VISIBLE_AROLL_SECONDS = BROLL_GEOMETRY_POLICY.min_visible_aroll_seconds
BROLL_MAX_PAD_SECONDS = BROLL_GEOMETRY_POLICY.max_pad_seconds


def _seconds_to_frame(seconds: float, fps: int) -> int:
    """Seconds -> frame index, round-half-up.

    Mirrors ``planning.editing.frame_grid.frame_index`` (the canonical 30fps grid) and
    the production ``_timeline_grid.to_frame`` so plan-time b-roll frames land on the
    exact same grid the renderer slices on.
    """
    return max(0, int(math.floor(float(seconds) * int(fps) + 0.5)))


@dataclass(frozen=True)
class BrollInsertion:
    asset_id: str
    clip_id: str
    timeline_start: float
    timeline_end: float
    source_start: float
    source_end: float
    confidence: float
    matched_keywords: tuple[str, ...]
    scene_name: str
    reason: str
    diversity_key: str = ""
    # Frame-aligned authoritative boundaries on the 30fps grid (#105). Set by
    # ``legalize_broll_window_frames`` / ``align_insertions_to_portrait_cuts``;
    # ``pad_start``/``pad_end`` carry the cut-snap residual the renderer clone-pads.
    # Left None when no grid context is given (the seconds-only placement still
    # stands and downstream derives frames from seconds).
    timeline_start_frame: int | None = None
    timeline_end_frame: int | None = None
    source_start_frame: int | None = None
    source_end_frame: int | None = None
    pad_start: float = 0.0
    pad_end: float = 0.0
    placement: str | None = None


@dataclass(frozen=True)
class BrollWindowPlacement:
    start_frame: int
    end_frame: int
    source_length_frames: int
    pad_start: float = 0.0
    pad_end: float = 0.0


def align_insertions_to_portrait_cuts(
    insertions: Sequence[BrollInsertion],
    *,
    fps: int,
    portrait_cut_frames: Sequence[int],
    min_visible_residual_frames: int | None = None,
    max_gap_frames: int = BROLL_PORTRAIT_CUT_SNAP_MAX_FRAMES,
    max_pad_seconds: float = BROLL_MAX_PAD_SECONDS,
) -> list[BrollInsertion]:
    """Frame-align b-roll inserts to the portrait cut grid at PLAN time (#105).

    Replaces the old downstream ``_timeline_grid.align_broll_to_portrait_cuts`` snap:
    the frame-grid constraint is now enforced where placement is decided, so the
    timeline node can stay verify-only. Each insert is quantized onto the fixed grid;
    when a timeline boundary lands a few frames inside a portrait shot (leaving a
    sliver of portrait too short to read), the boundary is snapped to the portrait cut
    and the extension is recorded as ``pad`` — the SOURCE window is never pulled past
    its clean span, so the renderer clone-pads the held frame instead of over-trimming.
    A candidate snap is dropped (boundary kept at its quantized seconds position) when
    it would invert the window, overlap a neighbouring insert, or need more clone-pad
    than the cap allows. narration semantic placement decided upstream is preserved.

    Always returns inserts with authoritative ``*_frame`` fields populated (snap or
    not); ``portrait_cut_frames`` is the sorted set of portrait segment boundary frames
    (contiguous, so consecutive pairs reconstruct each portrait shot window).
    """
    ordered = list(insertions)
    if not ordered:
        return ordered

    residual_limit = (
        max(0, int(min_visible_residual_frames))
        if min_visible_residual_frames is not None
        else _seconds_to_frame(BROLL_MIN_VISIBLE_AROLL_SECONDS, fps)
    )
    max_pad_seconds = max(0.0, float(max_pad_seconds))
    cuts = sorted({int(frame) for frame in portrait_cut_frames})
    windows = [(start, end) for start, end in zip(cuts, cuts[1:]) if end > start]
    snapping_enabled = (
        max_gap_frames > 0 and residual_limit > 0 and max_pad_seconds > 0 and bool(windows)
    )

    def _should_snap(residual_frames: int) -> bool:
        if residual_frames <= 0:
            return False
        required_pad_seconds = residual_frames / fps
        return (
            residual_frames < residual_limit
            and residual_frames <= max_gap_frames
            and required_pad_seconds <= max_pad_seconds
        )

    # Quantize every insert to the grid up front so neighbour-overlap guards compare
    # against ORIGINAL (pre-snap) positions, exactly like the old helper did.
    quantized = [
        (
            _seconds_to_frame(ins.timeline_start, fps),
            _seconds_to_frame(ins.timeline_end, fps),
            _seconds_to_frame(ins.source_start, fps),
            _seconds_to_frame(ins.source_end, fps),
        )
        for ins in ordered
    ]

    aligned: list[BrollInsertion] = []
    for index, ins in enumerate(ordered):
        start_frame, end_frame, source_start_frame, source_end_frame = quantized[index]
        new_start, new_end = start_frame, end_frame
        if snapping_enabled and end_frame > start_frame:
            for portrait_start, portrait_end in windows:
                if portrait_end <= new_start or portrait_start >= new_end:
                    continue
                if portrait_start < new_start < portrait_end and _should_snap(
                    new_start - portrait_start
                ):
                    new_start = portrait_start
                if portrait_start < new_end < portrait_end and _should_snap(
                    portrait_end - new_end
                ):
                    new_end = portrait_end
            preceding_end = quantized[index - 1][1] if index > 0 else None
            following_start = quantized[index + 1][0] if index + 1 < len(quantized) else None
            if (
                new_end <= new_start
                or (preceding_end is not None and new_start < preceding_end)
                or (following_start is not None and new_end > following_start)
            ):
                new_start, new_end = start_frame, end_frame

        pad_start = round((start_frame - new_start) / fps, 6) if new_start < start_frame else 0.0
        pad_end = round((new_end - end_frame) / fps, 6) if new_end > end_frame else 0.0
        aligned.append(
            replace(
                ins,
                timeline_start=round(new_start / fps, 3),
                timeline_end=round(new_end / fps, 3),
                timeline_start_frame=new_start,
                timeline_end_frame=new_end,
                source_start_frame=source_start_frame,
                source_end_frame=source_end_frame,
                pad_start=round(ins.pad_start + pad_start, 6),
                pad_end=round(ins.pad_end + pad_end, 6),
            )
        )
    return aligned


def _portrait_windows_from_cuts(portrait_cut_frames: Sequence[int]) -> list[tuple[int, int]]:
    cuts = sorted({int(frame) for frame in portrait_cut_frames})
    return [(start, end) for start, end in zip(cuts, cuts[1:]) if end > start]


def _resolved_min_visible_residual_frames(
    *,
    fps: int,
    min_visible_residual_frames: int | None,
) -> int:
    return (
        max(0, int(min_visible_residual_frames))
        if min_visible_residual_frames is not None
        else _seconds_to_frame(BROLL_MIN_VISIBLE_AROLL_SECONDS, fps)
    )


def _frame_bounds(ins: BrollInsertion, *, fps: int) -> tuple[int, int]:
    start = (
        int(ins.timeline_start_frame)
        if ins.timeline_start_frame is not None
        else _seconds_to_frame(ins.timeline_start, fps)
    )
    end = (
        int(ins.timeline_end_frame)
        if ins.timeline_end_frame is not None
        else _seconds_to_frame(ins.timeline_end, fps)
    )
    return start, end


def _has_short_visible_portrait_gap(
    insertions: Sequence[BrollInsertion],
    *,
    fps: int,
    portrait_cut_frames: Sequence[int],
    min_visible_residual_frames: int | None,
) -> bool:
    """Return True when b-roll creates an unsnappable, too-short A-roll sliver.

    The policy has two legal shapes:
      - leave at least ``BROLL_MIN_VISIBLE_AROLL_SECONDS`` of portrait visible; or
      - land close enough to a portrait cut for ``align_insertions_to_portrait_cuts``
        to snap and clone-pad within ``BROLL_MAX_PAD_SECONDS``.

    After alignment, any remaining visible portrait gap shorter than the minimum is
    therefore an illegal flash/sliver and the candidate should be skipped at planning
    time instead of relying on the renderer to hide it.
    """
    windows = _portrait_windows_from_cuts(portrait_cut_frames)
    if not insertions or not windows:
        return False
    residual_limit = _resolved_min_visible_residual_frames(
        fps=fps,
        min_visible_residual_frames=min_visible_residual_frames,
    )
    if residual_limit <= 0:
        return False

    frame_bounds = [_frame_bounds(ins, fps=fps) for ins in insertions]
    for portrait_start, portrait_end in windows:
        overlaps: list[tuple[int, int]] = []
        for start, end in frame_bounds:
            clipped_start = max(portrait_start, start)
            clipped_end = min(portrait_end, end)
            if clipped_end > clipped_start:
                overlaps.append((clipped_start, clipped_end))
        if not overlaps:
            continue

        overlaps.sort()
        merged: list[tuple[int, int]] = []
        for start, end in overlaps:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))

        cursor = portrait_start
        for start, end in merged:
            gap = start - cursor
            if 0 < gap < residual_limit:
                return True
            cursor = max(cursor, end)
        tail_gap = portrait_end - cursor
        if 0 < tail_gap < residual_limit:
            return True
    return False


def legalize_broll_window_frames(
    *,
    start_frame: int,
    end_frame: int,
    fps: int,
    portrait_cut_frames: Sequence[int],
    policy: BrollGeometryPolicy = BROLL_GEOMETRY_POLICY,
) -> BrollWindowPlacement | None:
    """Apply the shared B-roll sliver/snap legality gate to a frame window.

    Legal windows either leave at least ``min_visible_aroll_seconds`` of portrait
    around an interior boundary, or snap a sub-``max_pad_seconds`` residual to the
    portrait cut. Any remaining in-between residual is an illegal flash frame.
    """
    fps = max(1, int(fps))
    start_frame = int(start_frame)
    end_frame = int(end_frame)
    source_length_frames = end_frame - start_frame
    min_insert_frames = _seconds_to_frame(policy.min_insert_seconds, fps)
    if source_length_frames < min_insert_frames:
        return None

    insert = BrollInsertion(
        asset_id="window",
        clip_id="window",
        timeline_start=round(start_frame / fps, 6),
        timeline_end=round(end_frame / fps, 6),
        source_start=0.0,
        source_end=round(source_length_frames / fps, 6),
        confidence=1.0,
        matched_keywords=(),
        scene_name="",
        reason="window legality",
    )
    min_visible_residual_frames = _seconds_to_frame(policy.min_visible_aroll_seconds, fps)
    aligned = align_insertions_to_portrait_cuts(
        [insert],
        fps=fps,
        portrait_cut_frames=portrait_cut_frames,
        min_visible_residual_frames=min_visible_residual_frames,
        max_gap_frames=policy.snap_max_frames,
        max_pad_seconds=policy.max_pad_seconds,
    )
    if _has_short_visible_portrait_gap(
        aligned,
        fps=fps,
        portrait_cut_frames=portrait_cut_frames,
        min_visible_residual_frames=min_visible_residual_frames,
    ):
        return None
    [placement] = aligned
    legalized_start, legalized_end = _frame_bounds(placement, fps=fps)
    source_start = placement.source_start_frame
    source_end = placement.source_end_frame
    legalized_source_length = (
        max(0, int(source_end) - int(source_start))
        if source_start is not None and source_end is not None
        else source_length_frames
    )
    if legalized_end - legalized_start < min_insert_frames:
        return None
    if legalized_source_length < min_insert_frames:
        return None
    return BrollWindowPlacement(
        start_frame=legalized_start,
        end_frame=legalized_end,
        source_length_frames=legalized_source_length,
        pad_start=placement.pad_start,
        pad_end=placement.pad_end,
    )


