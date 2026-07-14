"""Real b-roll insertion planning: place ranked clips inside narration windows.

Replaces the seeded ``start_sec = index * 3`` placement. Each chosen candidate
is anchored to the narration beat it best matched (so the insert lands inside a
real spoken window, not a mechanical 0/3/6 grid), with non-overlapping timeline
windows and the source trim taken from the matched clip. Pure + deterministic.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.material.broll_pack import BrollCandidate


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

_MIN_INSERT_SECONDS = BROLL_GEOMETRY_POLICY.min_insert_seconds
_MAX_INSERT_SECONDS = BROLL_GEOMETRY_POLICY.max_insert_seconds

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
class CoverageSegment:
    asset_id: str
    clip_id: str
    timeline_start: float
    timeline_end: float
    source_start: float
    source_end: float
    reason: str
    confidence: float
    matched_keywords: tuple[str, ...]
    scene_name: str
    diversity_key: str


@dataclass(frozen=True)
class CoveragePlan:
    segments: tuple[CoverageSegment, ...]
    covered_sec: float
    sufficient: bool


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
    # ``plan_insertions`` when ``fps`` + ``portrait_cut_frames`` are supplied (the
    # digital_human_v2 path); ``pad_start``/``pad_end`` carry the cut-snap residual the
    # renderer clone-pads. Left None when no grid context is given (the seconds-only
    # placement still stands and downstream derives frames from seconds).
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


def _coverage_reason(candidate: BrollCandidate, units: Sequence[NarrationUnit]) -> str:
    if candidate.best_segment is not None:
        return f"cover full narration near '{candidate.best_segment.text[:24]}'"
    if units:
        return f"cover full narration near '{units[0].text[:24]}'"
    return "cover full narration"


def plan_coverage(
    *,
    candidates: Sequence[BrollCandidate],
    units: Sequence[NarrationUnit],
    target_sec: float,
    min_segment_duration: float,
    tolerance_sec: float = 0.04,
    freshness_seed: str | None = None,
) -> CoveragePlan:
    """Plan deterministic b-roll coverage over ``[0, target_sec]`` from ranked clips."""
    target = max(0.0, float(target_sec))
    min_duration = max(0.0, float(min_segment_duration))
    tolerance = max(0.0, float(tolerance_sec))
    if target <= tolerance:
        return CoveragePlan(segments=(), covered_sec=0.0, sufficient=True)

    segments: list[CoverageSegment] = []
    cursor = 0.0
    remaining_candidates = _fresh_candidate_order(candidates, freshness_seed=freshness_seed)
    used_clips: set[tuple[str, str]] = set()
    used_assets: set[str] = set()
    used_diversity_keys: set[str] = set()

    while cursor < target - tolerance:
        selection = _select_coverage_candidate(
            remaining_candidates,
            used_clips=used_clips,
            used_assets=used_assets,
            used_diversity_keys=used_diversity_keys,
            cursor=cursor,
            target=target,
            min_duration=min_duration,
            tolerance=tolerance,
        )
        if selection is None:
            break
        candidate_index, candidate = selection
        del remaining_candidates[candidate_index]

        source_start = float(candidate.source_start)
        source_end = float(candidate.source_end)
        available = max(0.0, source_end - source_start)
        remaining = target - cursor
        length = min(available, remaining)
        if length <= 0:
            continue

        timeline_start = round(cursor, 3)
        timeline_end = round(min(target, cursor + length), 3)
        taken = timeline_end - timeline_start
        trim_start = _fresh_source_start(
            candidate,
            taken=taken,
            freshness_seed=freshness_seed,
            parts=("coverage", len(segments)),
        )
        trim_end = round(min(source_end, trim_start + taken), 3)
        segments.append(
            CoverageSegment(
                asset_id=candidate.asset_id,
                clip_id=candidate.clip_id,
                timeline_start=timeline_start,
                timeline_end=timeline_end,
                source_start=round(trim_start, 3),
                source_end=trim_end,
                reason=_coverage_reason(candidate, units),
                confidence=round(min(1.0, candidate.score / 100.0), 3),
                matched_keywords=candidate.matched_keywords,
                scene_name=candidate.scene_name,
                diversity_key=candidate.diversity_key,
            )
        )
        used_clips.add((candidate.asset_id, candidate.clip_id))
        used_assets.add(candidate.asset_id)
        if candidate.diversity_key:
            used_diversity_keys.add(candidate.diversity_key)
        cursor = timeline_end

    covered = min(target, cursor)
    return CoveragePlan(
        segments=tuple(segments),
        covered_sec=round(covered, 3),
        sufficient=covered >= target - tolerance,
    )


def _select_coverage_candidate(
    candidates: Sequence[BrollCandidate],
    *,
    used_clips: set[tuple[str, str]],
    used_assets: set[str],
    used_diversity_keys: set[str],
    cursor: float,
    target: float,
    min_duration: float,
    tolerance: float,
) -> tuple[int, BrollCandidate] | None:
    """Pick the next coverage clip with diversity constraints that relax in phases."""
    for phase in range(4):
        for index, candidate in enumerate(candidates):
            if (candidate.asset_id, candidate.clip_id) in used_clips:
                continue
            if not _passes_diversity_phase(candidate, used_assets, used_diversity_keys, phase):
                continue
            if not _has_usable_span(
                candidate,
                cursor=cursor,
                target=target,
                min_duration=min_duration,
                tolerance=tolerance,
            ):
                continue
            return index, candidate
    return None


def _passes_diversity_phase(
    candidate: BrollCandidate,
    used_assets: set[str],
    used_diversity_keys: set[str],
    phase: int,
) -> bool:
    same_asset = candidate.asset_id in used_assets
    same_diversity = bool(candidate.diversity_key) and candidate.diversity_key in used_diversity_keys
    if phase == 0:
        return not same_asset and not same_diversity
    if phase == 1:
        return not same_asset
    if phase == 2:
        return not same_diversity
    return True


def _is_reused_candidate(
    candidate: BrollCandidate,
    *,
    used_clips: set[tuple[str, str]],
    used_diversity_keys: set[str],
) -> bool:
    if (candidate.asset_id, candidate.clip_id) in used_clips:
        return True
    return bool(candidate.diversity_key) and candidate.diversity_key in used_diversity_keys


def _has_usable_span(
    candidate: BrollCandidate,
    *,
    cursor: float,
    target: float,
    min_duration: float,
    tolerance: float,
) -> bool:
    source_start = float(candidate.source_start)
    source_end = float(candidate.source_end)
    available = max(0.0, source_end - source_start)
    remaining = target - cursor
    return available > 0 and (available >= min_duration or available >= remaining - tolerance)


def _seed_fraction(seed: str | None, *parts: object) -> float:
    if not seed:
        return 0.0
    payload = "|".join([seed, *(str(part) for part in parts)]).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _fresh_candidate_order(
    candidates: Sequence[BrollCandidate], *, freshness_seed: str | None
) -> list[BrollCandidate]:
    ordered = list(candidates)
    if not freshness_seed:
        return ordered

    band_size = 4
    fresh: list[BrollCandidate] = []
    for band_start in range(0, len(ordered), band_size):
        band = ordered[band_start : band_start + band_size]
        band.sort(
            key=lambda candidate: _seed_fraction(
                freshness_seed,
                "candidate_order",
                band_start // band_size,
                candidate.asset_id,
                candidate.clip_id,
                candidate.diversity_key,
            )
        )
        fresh.extend(band)
    return fresh


def _fresh_source_start(
    candidate: BrollCandidate,
    *,
    taken: float,
    freshness_seed: str | None,
    parts: tuple[object, ...],
) -> float:
    start = float(candidate.source_start)
    end = float(candidate.source_end)
    if not freshness_seed:
        return start
    slack = max(0.0, end - start - max(0.0, taken))
    if slack <= 0:
        return start
    offset = slack * _seed_fraction(
        freshness_seed,
        "source_trim",
        candidate.asset_id,
        candidate.clip_id,
        *parts,
    )
    return min(end - max(0.0, taken), start + offset)


def _even_indices(total: int, count: int) -> list[int]:
    """``count`` evenly-spaced, *centered* indices into a ``total``-length sequence.

    Centered (``(2i+1)*total // (2*count)``) so the first pick can land at index 0
    and the spread covers the whole range — used to sprinkle a few generic fillers
    across the eligible narration windows without always skipping the opener.
    """
    if total <= 0 or count <= 0:
        return []
    return [((2 * i + 1) * total) // (2 * count) for i in range(count)]


def _build_insertion(
    candidate: BrollCandidate,
    *,
    host_unit: NarrationUnit,
    start: float,
    available: float,
    timeline_end: float,
    freshness_seed: str | None,
    index: int,
) -> BrollInsertion | None:
    """Build one insert inside ``host_unit`` at/after ``start``.

    Length is clamped to ``[_MIN_INSERT_SECONDS, available]`` (the caller
    guarantees ``available >= _MIN_INSERT_SECONDS``) so the insert never spills
    past its window; freshness jitter + source trim stay deterministic per run id.
    Returns ``None`` if the placement would round past the timeline end.
    """
    clip_span = max(0.0, candidate.source_end - candidate.source_start)
    # A usable source span shorter than the minimum insert can only fill the slot
    # by reading past its clean span (into avoided footage / EOF), so skip it
    # rather than over-trim — more likely now that short clean clips with no
    # keyword match are admitted as generic fillers. (clip_span == 0 means the
    # caller left the source window open and falls back to _MAX below.)
    if 0.0 < clip_span < _MIN_INSERT_SECONDS:
        return None
    host_end = min(host_unit.end, timeline_end)
    desired = clip_span if clip_span > 0 else _MAX_INSERT_SECONDS
    length = max(_MIN_INSERT_SECONDS, min(desired, _MAX_INSERT_SECONDS, available))
    start = _fresh_timeline_start(
        start,
        length=length,
        host_end=host_end,
        freshness_seed=freshness_seed,
        parts=(candidate.asset_id, candidate.clip_id, index),
    )
    # Clamp against the host window end (not just timeline_end) so freshness jitter
    # / rounding can never spill the overlay past its narration window.
    end = min(round(start + length, 3), round(host_end, 3))
    if end - start < _MIN_INSERT_SECONDS:
        return None
    source_start = _fresh_source_start(
        candidate, taken=length, freshness_seed=freshness_seed, parts=("insert", index)
    )
    source_end = round(source_start + length, 3)
    beat = candidate.best_segment
    return BrollInsertion(
        asset_id=candidate.asset_id,
        clip_id=candidate.clip_id,
        timeline_start=round(start, 3),
        timeline_end=end,
        source_start=round(source_start, 3),
        source_end=source_end,
        confidence=round(min(1.0, candidate.score / 100.0), 3),
        matched_keywords=candidate.matched_keywords,
        scene_name=candidate.scene_name,
        reason=(
            f"matched narration beat '{beat.text[:24]}'"
            if beat is not None
            else "anchored to narration window"
        ),
        diversity_key=candidate.diversity_key,
    )


def _unit_for_time(units: Sequence[NarrationUnit], t: float) -> NarrationUnit | None:
    for unit in units:
        if unit.start <= t < unit.end:
            return unit
    return units[0] if units else None


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


def _align_insertions_to_grid_if_safe(
    insertions: Sequence[BrollInsertion],
    *,
    fps: int | None,
    portrait_cut_frames: Sequence[int] | None,
    min_visible_residual_frames: int | None,
    max_gap_frames: int = BROLL_PORTRAIT_CUT_SNAP_MAX_FRAMES,
    max_pad_seconds: float = BROLL_MAX_PAD_SECONDS,
) -> list[BrollInsertion] | None:
    ordered = sorted(insertions, key=lambda ins: ins.timeline_start)
    if fps is None or portrait_cut_frames is None:
        return ordered

    aligned = align_insertions_to_portrait_cuts(
        ordered,
        fps=fps,
        portrait_cut_frames=portrait_cut_frames,
        min_visible_residual_frames=min_visible_residual_frames,
        max_gap_frames=max_gap_frames,
        max_pad_seconds=max_pad_seconds,
    )
    if _has_short_visible_portrait_gap(
        aligned,
        fps=fps,
        portrait_cut_frames=portrait_cut_frames,
        min_visible_residual_frames=min_visible_residual_frames,
    ):
        return None
    return aligned


def _timeline_start_variants(
    insert: BrollInsertion,
    *,
    start: float,
    host_end: float,
    fps: int | None,
    portrait_cut_frames: Sequence[int] | None,
    min_visible_residual_frames: int | None,
) -> list[BrollInsertion]:
    length = round(insert.timeline_end - insert.timeline_start, 6)
    latest_start = round(host_end - length, 6)
    if length <= 0 or latest_start < start:
        return [insert]
    if fps is None or portrait_cut_frames is None:
        return [insert]

    residual_frames = _resolved_min_visible_residual_frames(
        fps=fps,
        min_visible_residual_frames=min_visible_residual_frames,
    )
    residual_seconds = residual_frames / fps if fps > 0 else BROLL_MIN_VISIBLE_AROLL_SECONDS
    candidates = {
        round(insert.timeline_start, 3),
        round(start, 3),
        round(latest_start, 3),
    }
    for portrait_start, portrait_end in _portrait_windows_from_cuts(portrait_cut_frames):
        portrait_start_seconds = portrait_start / fps
        portrait_end_seconds = portrait_end / fps
        candidates.update(
            {
                round(portrait_start_seconds, 3),
                round(portrait_end_seconds - length, 3),
                round(portrait_start_seconds + residual_seconds, 3),
                round(portrait_end_seconds - residual_seconds - length, 3),
            }
        )

    ordered_starts = sorted(
        {
            candidate
            for candidate in candidates
            if candidate >= round(start, 3) and candidate <= round(latest_start, 3)
        },
        key=lambda candidate: (abs(candidate - insert.timeline_start), candidate),
    )
    variants: list[BrollInsertion] = []
    for candidate_start in ordered_starts:
        variants.append(
            replace(
                insert,
                timeline_start=round(candidate_start, 3),
                timeline_end=round(candidate_start + length, 3),
                timeline_start_frame=None,
                timeline_end_frame=None,
                source_start_frame=None,
                source_end_frame=None,
                pad_start=0.0,
                pad_end=0.0,
            )
        )
    return variants or [insert]


def _accept_insertion_if_safe(
    insertions: Sequence[BrollInsertion],
    insert: BrollInsertion,
    *,
    start: float,
    host_end: float,
    fps: int | None,
    portrait_cut_frames: Sequence[int] | None,
    min_visible_residual_frames: int | None,
    max_gap_frames: int = BROLL_PORTRAIT_CUT_SNAP_MAX_FRAMES,
    max_pad_seconds: float = BROLL_MAX_PAD_SECONDS,
) -> list[BrollInsertion] | None:
    for variant in _timeline_start_variants(
        insert,
        start=start,
        host_end=host_end,
        fps=fps,
        portrait_cut_frames=portrait_cut_frames,
        min_visible_residual_frames=min_visible_residual_frames,
    ):
        accepted = _align_insertions_to_grid_if_safe(
            [*insertions, variant],
            fps=fps,
            portrait_cut_frames=portrait_cut_frames,
            min_visible_residual_frames=min_visible_residual_frames,
            max_gap_frames=max_gap_frames,
            max_pad_seconds=max_pad_seconds,
        )
        if accepted is not None:
            return accepted
    return None


def place_insertion_safely(
    existing: Sequence[BrollInsertion],
    insert: BrollInsertion,
    *,
    window_start: float,
    window_end: float,
    fps: int | None,
    portrait_cut_frames: Sequence[int] | None,
    policy: BrollGeometryPolicy = BROLL_GEOMETRY_POLICY,
) -> list[BrollInsertion] | None:
    min_visible_residual_frames = (
        _seconds_to_frame(policy.min_visible_aroll_seconds, fps) if fps is not None else None
    )
    return _accept_insertion_if_safe(
        existing,
        insert,
        start=window_start,
        host_end=window_end,
        fps=fps,
        portrait_cut_frames=portrait_cut_frames,
        min_visible_residual_frames=min_visible_residual_frames,
        max_gap_frames=policy.snap_max_frames,
        max_pad_seconds=policy.max_pad_seconds,
    )


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


def plan_insertions(
    *,
    candidates: Sequence[BrollCandidate],
    units: Sequence[NarrationUnit],
    max_inserts: int,
    freshness_seed: str | None = None,
    fps: int | None = None,
    portrait_cut_frames: Sequence[int] | None = None,
    min_visible_residual_frames: int | None = None,
) -> list[BrollInsertion]:
    """Plan up to ``max_inserts`` b-roll inserts across the narration windows.

    Two passes so a real keyword match is never starved by a generic filler:
      1. keyword-matched candidates (``best_segment`` set) anchor inside the beat
         they matched, in score/freshness order, dropping any whose beat is too
         short rather than spilling past it;
      2. leftover slots are filled with anchorless generic clips, one per still-
         empty narration window, sprinkled evenly across the timeline.
    Inserts never overlap (at most one per window) and never spill past their
    window. Returns an empty list when there are no candidates, no narration, or
    no room (honest: the caller soft-degrades rather than fabricating placements).

    When ``fps`` and ``portrait_cut_frames`` are supplied (the digital_human_v2 path),
    each insert is additionally frame-aligned to the portrait cut grid at plan time
    (#105) via :func:`align_insertions_to_portrait_cuts`, so the returned inserts carry
    authoritative ``*_frame`` boundaries (+ clone-pad residual) and the timeline node
    only validates + assembles them.
    """
    unit_list = [u for u in units if u.end > u.start]
    if not unit_list or max_inserts <= 0:
        return []

    timeline_end = max(u.end for u in unit_list)
    insertions: list[BrollInsertion] = []
    used_clips: set[tuple[str, str]] = set()
    used_diversity_keys: set[str] = set()
    occupied_units: set[str] = set()
    ordered = _fresh_candidate_order(candidates, freshness_seed=freshness_seed)

    # Phase 1 — keyword-matched candidates claim the beat they matched. Processed
    # before (and never displaced by) generic fillers; the cursor keeps Phase-1
    # inserts non-overlapping and after each prior match.
    deferred: list[BrollCandidate] = []
    cursor = 0.0
    for candidate in ordered:
        if len(insertions) >= max_inserts:
            break
        key = (candidate.asset_id, candidate.clip_id)
        if key in used_clips:
            continue
        if candidate.best_segment is None:
            deferred.append(candidate)
            continue
        if _is_reused_candidate(
            candidate,
            used_clips=used_clips,
            used_diversity_keys=used_diversity_keys,
        ):
            continue
        anchor = candidate.best_segment.start
        host_unit = _unit_for_time(unit_list, anchor)
        if host_unit is None:
            continue
        start = max(anchor, cursor, host_unit.start)
        if start >= host_unit.end or start >= timeline_end:
            continue
        available = min(host_unit.end, timeline_end) - start
        if available < _MIN_INSERT_SECONDS:
            continue
        insert = _build_insertion(
            candidate,
            host_unit=host_unit,
            start=start,
            available=available,
            timeline_end=timeline_end,
            freshness_seed=freshness_seed,
            index=len(insertions),
        )
        if insert is None:
            continue
        accepted = _accept_insertion_if_safe(
            insertions,
            insert,
            start=start,
            host_end=min(host_unit.end, timeline_end),
            fps=fps,
            portrait_cut_frames=portrait_cut_frames,
            min_visible_residual_frames=min_visible_residual_frames,
        )
        if accepted is None:
            continue
        insertions = accepted
        used_clips.add(key)
        if candidate.diversity_key:
            used_diversity_keys.add(candidate.diversity_key)
        occupied_units.add(host_unit.unit_id)
        cursor = max(ins.timeline_end for ins in insertions)

    # Phase 2 — sprinkle leftover slots with generic fillers, one per still-empty
    # window, evenly spaced. Empty windows are reachable regardless of the Phase-1
    # cursor, so a late keyword match never suppresses earlier fillers.
    remaining = max_inserts - len(insertions)
    if remaining > 0 and deferred:
        eligible = [
            u
            for u in unit_list
            if u.unit_id not in occupied_units
            and min(u.end, timeline_end) - u.start >= _MIN_INSERT_SECONDS
        ]
        gi = 0
        for idx in _even_indices(len(eligible), min(remaining, len(eligible))):
            unit = eligible[idx]
            while gi < len(deferred) and (deferred[gi].asset_id, deferred[gi].clip_id) in used_clips:
                gi += 1
            if gi >= len(deferred):
                break
            candidate = deferred[gi]
            gi += 1
            insert = _build_insertion(
                candidate,
                host_unit=unit,
                start=unit.start,
                available=min(unit.end, timeline_end) - unit.start,
                timeline_end=timeline_end,
                freshness_seed=freshness_seed,
                index=len(insertions),
            )
            if insert is None:
                continue
            accepted = _accept_insertion_if_safe(
                insertions,
                insert,
                start=unit.start,
                host_end=min(unit.end, timeline_end),
                fps=fps,
                portrait_cut_frames=portrait_cut_frames,
                min_visible_residual_frames=min_visible_residual_frames,
            )
            if accepted is None:
                continue
            insertions = accepted
            used_clips.add((candidate.asset_id, candidate.clip_id))

    aligned = _align_insertions_to_grid_if_safe(
        insertions,
        fps=fps,
        portrait_cut_frames=portrait_cut_frames,
        min_visible_residual_frames=min_visible_residual_frames,
    )
    return aligned or []


def _fresh_timeline_start(
    start: float,
    *,
    length: float,
    host_end: float,
    freshness_seed: str | None,
    parts: tuple[object, ...],
) -> float:
    if not freshness_seed:
        return start
    slack = max(0.0, host_end - start - length)
    if slack <= 0:
        return start
    offset = slack * _seed_fraction(freshness_seed, "timeline_start", *parts)
    return round(min(host_end - length, start + offset), 3)
