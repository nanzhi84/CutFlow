"""Real b-roll insertion planning: place ranked clips inside narration windows.

Replaces the seeded ``start_sec = index * 3`` placement. Each chosen candidate
is anchored to the narration beat it best matched (so the insert lands inside a
real spoken window, not a mechanical 0/3/6 grid), with non-overlapping timeline
windows and the source trim taken from the matched clip. Pure + deterministic.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.material.broll_pack import BrollCandidate

_MIN_INSERT_SECONDS = 1.5
_MAX_INSERT_SECONDS = 4.0


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
    desired = clip_span if clip_span > 0 else _MAX_INSERT_SECONDS
    length = max(_MIN_INSERT_SECONDS, min(desired, _MAX_INSERT_SECONDS, available))
    start = _fresh_timeline_start(
        start,
        length=length,
        host_end=min(host_unit.end, timeline_end),
        freshness_seed=freshness_seed,
        parts=(candidate.asset_id, candidate.clip_id, index),
    )
    end = round(start + length, 3)
    if end > timeline_end:
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


def plan_insertions(
    *,
    candidates: Sequence[BrollCandidate],
    units: Sequence[NarrationUnit],
    max_inserts: int,
    freshness_seed: str | None = None,
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
    """
    unit_list = [u for u in units if u.end > u.start]
    if not unit_list or max_inserts <= 0:
        return []

    timeline_end = max(u.end for u in unit_list)
    insertions: list[BrollInsertion] = []
    used_clips: set[tuple[str, str]] = set()
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
        insertions.append(insert)
        used_clips.add(key)
        occupied_units.add(host_unit.unit_id)
        cursor = insert.timeline_end

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
            insertions.append(insert)
            used_clips.add((candidate.asset_id, candidate.clip_id))

    insertions.sort(key=lambda ins: ins.timeline_start)
    return insertions


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
