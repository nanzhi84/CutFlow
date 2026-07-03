"""Window-scoped deterministic candidate shortlisting."""

from __future__ import annotations

from typing import Any

from packages.planning.editing.frame_grid import TIMELINE_FPS, frame_index


def shortlist_for_windows(
    portrait_windows: list[dict],
    broll_windows: list[dict],
    material_candidates: dict,
    *,
    portrait_per_window: int = 12,
    broll_per_window: int = 6,
) -> tuple[dict, dict[str, dict[str, int]]]:
    portrait, portrait_counts = _shortlist_medium(
        windows=portrait_windows,
        candidates=_candidate_list(material_candidates, "portrait_candidates"),
        per_window=portrait_per_window,
        eligible=_portrait_eligible,
    )
    broll, broll_counts = _shortlist_medium(
        windows=broll_windows,
        candidates=_candidate_list(material_candidates, "broll_candidates"),
        per_window=broll_per_window,
        eligible=_broll_eligible,
    )
    shortlisted = dict(material_candidates)
    shortlisted["portrait_candidates"] = portrait
    shortlisted["broll_candidates"] = broll
    return shortlisted, {"portrait": portrait_counts, "broll": broll_counts}


def _shortlist_medium(
    *,
    windows: list[dict],
    candidates: list[dict],
    per_window: int,
    eligible,
) -> tuple[list[dict], dict[str, int]]:
    raw = len(candidates)
    eligible_indices: set[int] = set()
    exposed_indices: set[int] = set()
    for window in windows:
        ranked = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if eligible(window, candidate)
        ]
        ranked.sort(key=lambda item: (-_score(item[1]), _candidate_key(item[1], item[0])))
        eligible_indices.update(index for index, _candidate in ranked)
        exposed_indices.update(index for index, _candidate in ranked[: max(0, per_window)])
    exposed = [
        candidate
        for index, candidate in sorted(
            ((index, candidates[index]) for index in exposed_indices),
            key=lambda item: (-_score(item[1]), _candidate_key(item[1], item[0])),
        )
    ]
    return exposed, {
        "raw": raw,
        "eligible": len(eligible_indices),
        "exposed": len(exposed),
        "dropped": max(0, raw - len(exposed)),
    }


def _candidate_list(material: dict, key: str) -> list[dict]:
    return [
        item
        for item in (material.get(key) or [])
        if isinstance(item, dict) and item.get("asset_id")
    ]


def _portrait_eligible(window: dict, candidate: dict) -> bool:
    return _source_frames_available(candidate) >= _window_required_frames(window)


def _broll_eligible(window: dict, candidate: dict) -> bool:
    if _window_required_frames(window) / TIMELINE_FPS < 1.5:
        return False
    available = _source_seconds_available(candidate)
    return available <= 0.0 or available >= 1.5


def _window_required_frames(window: dict) -> int:
    return max(0, int(window.get("end_frame", 0) or 0) - int(window.get("start_frame", 0) or 0))


def _source_frames_available(candidate: dict) -> int:
    meta = _meta(candidate)
    start = _as_float(meta.get("source_start"))
    end = _as_float(meta.get("source_end"))
    if end <= start:
        return 0
    return frame_index(end) - frame_index(start)


def _source_seconds_available(candidate: dict) -> float:
    meta = _meta(candidate)
    start = _as_float(meta.get("source_start"))
    end = _as_float(meta.get("source_end"))
    return end - start


def _candidate_key(candidate: dict, index: int) -> str:
    meta = _meta(candidate)
    return "|".join(
        [
            str(candidate.get("asset_id") or ""),
            str(meta.get("clip_id") or ""),
            str(meta.get("source_start") or ""),
            str(meta.get("source_end") or ""),
            f"{index:06d}",
        ]
    )


def _meta(candidate: dict) -> dict[str, Any]:
    meta = candidate.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _score(candidate: dict) -> float:
    return _as_float(candidate.get("score"))


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
