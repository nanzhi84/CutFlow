"""Real b-roll insertion planning: place ranked clips inside narration windows.

Replaces the seeded ``start_sec = index * 3`` placement. Each chosen candidate
is anchored to the narration beat it best matched (so the insert lands inside a
real spoken window, not a mechanical 0/3/6 grid), with non-overlapping timeline
windows and the source trim taken from the matched clip. Pure + deterministic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.material.broll_pack import BrollCandidate

_MIN_INSERT_SECONDS = 1.5
_MAX_INSERT_SECONDS = 4.0


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
) -> list[BrollInsertion]:
    """Plan up to ``max_inserts`` b-roll inserts anchored in narration windows.

    Each insert is placed at the start of its matched narration beat (clamped so
    its window stays inside the beat and after any earlier insert). Returns an
    empty list when there are no candidates, no narration, or no room (honest:
    the caller soft-degrades rather than fabricating placements).
    """
    unit_list = [u for u in units if u.end > u.start]
    if not unit_list or max_inserts <= 0:
        return []

    timeline_end = max(u.end for u in unit_list)
    insertions: list[BrollInsertion] = []
    cursor = 0.0
    used_clips: set[tuple[str, str]] = set()

    for candidate in candidates:
        if len(insertions) >= max_inserts:
            break
        key = (candidate.asset_id, candidate.clip_id)
        if key in used_clips:
            continue

        beat = candidate.best_segment
        anchor = beat.start if beat is not None else cursor
        host_unit = _unit_for_time(unit_list, anchor)
        if host_unit is None:
            continue

        # Place inside the host narration window, after any prior insert.
        start = max(anchor, cursor, host_unit.start)
        if start >= host_unit.end or start >= timeline_end:
            continue

        clip_span = max(0.0, candidate.source_end - candidate.source_start)
        available = min(host_unit.end, timeline_end) - start
        # The matched beat must be long enough to hold a full minimum-length insert.
        # Otherwise skip this candidate rather than letting max(_MIN_INSERT_SECONDS, ...)
        # below push the insert past the beat into the next narration window (real
        # per-clause TTS beats are frequently sub-1.5s). The caller soft-degrades.
        if available < _MIN_INSERT_SECONDS:
            continue
        desired = clip_span if clip_span > 0 else _MAX_INSERT_SECONDS
        # available >= _MIN_INSERT_SECONDS, so length stays in [_MIN, available] and
        # end never spills past the host beat.
        length = max(_MIN_INSERT_SECONDS, min(desired, _MAX_INSERT_SECONDS, available))
        end = round(start + length, 3)
        if end > timeline_end:
            continue

        source_start = candidate.source_start
        source_end = round(source_start + length, 3)
        insertions.append(
            BrollInsertion(
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
        )
        used_clips.add(key)
        cursor = end

    return insertions
