"""Candidate indexing shared by deterministic editing planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _as_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


@dataclass(frozen=True)
class IndexedCandidates:
    portrait_by_id: dict[str, dict]
    broll_by_id: dict[str, dict]
    font_by_id: dict[str, dict]
    bgm_by_id: dict[str, dict]


def _candidate_list(material: dict, key: str) -> list[dict]:
    return [
        item
        for item in (material.get(key) or [])
        if isinstance(item, dict) and item.get("asset_id")
    ]


def index_candidates(material: dict) -> IndexedCandidates:
    """Assign stable IDs to every material-pack candidate."""

    portrait = _candidate_list(material, "portrait_candidates")
    broll = _candidate_list(material, "broll_candidates")
    font = _candidate_list(material, "font_candidates")
    bgm = _candidate_list(material, "bgm_candidates")
    return IndexedCandidates(
        portrait_by_id={f"pc_{i:03d}": cand for i, cand in enumerate(portrait)},
        broll_by_id={f"bc_{i:03d}": cand for i, cand in enumerate(broll)},
        font_by_id={_as_str(cand["asset_id"]): cand for cand in font},
        bgm_by_id={_as_str(cand["asset_id"]): cand for cand in bgm},
    )
