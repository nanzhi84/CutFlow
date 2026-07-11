"""Pure portrait/B-roll selection core for ``MediaSelectionAgentPlanning``.

The active v2 type surface contains no font, BGM, caption, or style fields. The
LLM may only fill authoritative media windows with candidate IDs; every timing
and geometry fact remains local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from packages.planning.editing.frame_grid import frame_index, to_seconds
from packages.planning.material import longest_clean_portrait_source_span

TIMELINE_FPS = 30
PORTRAIT_UNIQUENESS_RULE = (
    "同一个 asset_id 最多只能用于一个 portrait_slot；"
    "人像切镜窗口由 TimelineWindowPlanning 按 strict uniqueness 编译，Agent 禁止放宽复用。"
)
BROLL_INSERT_UNIQUENESS_RULE = (
    "当前是 insert 模式：除 candidate_id 不得重复外，非空 asset_id 与 diversity_key "
    "也都必须全局唯一；选择前请根据候选表中的 diversity_key 主动避开同类素材。"
)
BROLL_FULL_COVERAGE_UNIQUENESS_RULE = (
    "当前是 full_coverage 模式：candidate_id 仍不得重复；为保证逐窗覆盖，"
    "允许复用 asset_id 或 diversity_key。"
)


@dataclass(frozen=True)
class PortraitChoice:
    slot_id: str
    candidate_id: str
    reason: str = ""


@dataclass(frozen=True)
class BrollChoice:
    slot_id: str
    candidate_id: str
    reason: str = ""


@dataclass(frozen=True)
class MediaSelection:
    portrait: list[PortraitChoice] = field(default_factory=list)
    broll: list[BrollChoice] = field(default_factory=list)
    analysis: str = ""
    overreach_fields: tuple[str, ...] = ()
    parse_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaCandidates:
    portrait_by_id: dict[str, dict]
    broll_by_id: dict[str, dict]


_TOP_LEVEL_FIELDS = frozenset({"portrait_plan", "broll_plan", "analysis"})
_PORTRAIT_FIELDS = frozenset({"slot_id", "candidate_id", "reason"})
_BROLL_FIELDS = frozenset({"slot_id", "candidate_id", "reason"})


def _as_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _meta(candidate: dict) -> dict:
    value = candidate.get("metadata")
    return value if isinstance(value, dict) else {}


def parse_media_selection(output: Any) -> MediaSelection:
    if not isinstance(output, dict):
        return MediaSelection(parse_errors=("output must be a JSON object",))

    data = output
    overreach = [f"top_level.{key}" for key in sorted(set(data) - _TOP_LEVEL_FIELDS)]
    parse_errors: list[str] = []
    for field_name in sorted(_TOP_LEVEL_FIELDS - set(data)):
        parse_errors.append(f"missing top-level field '{field_name}'")

    portrait: list[PortraitChoice] = []
    raw_portrait = data.get("portrait_plan")
    if "portrait_plan" in data and not isinstance(raw_portrait, list):
        parse_errors.append("portrait_plan must be an array")
        raw_portrait = []
    for index, item in enumerate(raw_portrait or []):
        if not isinstance(item, dict):
            parse_errors.append(f"portrait_plan[{index}] must be an object")
            continue
        overreach.extend(
            f"portrait_plan[{index}].{key}" for key in sorted(set(item) - _PORTRAIT_FIELDS)
        )
        for field_name in sorted(_PORTRAIT_FIELDS - set(item)):
            parse_errors.append(f"portrait_plan[{index}] missing field '{field_name}'")
        slot_id = _strict_string(
            item.get("slot_id"), f"portrait_plan[{index}].slot_id", parse_errors
        )
        candidate_id = _strict_string(
            item.get("candidate_id"), f"portrait_plan[{index}].candidate_id", parse_errors
        )
        reason = _strict_string(
            item.get("reason"), f"portrait_plan[{index}].reason", parse_errors, allow_empty=True
        )
        if slot_id and candidate_id and reason is not None:
            portrait.append(
                PortraitChoice(
                    slot_id=slot_id,
                    candidate_id=candidate_id,
                    reason=reason,
                )
            )

    broll: list[BrollChoice] = []
    raw_broll = data.get("broll_plan")
    if "broll_plan" in data and not isinstance(raw_broll, list):
        parse_errors.append("broll_plan must be an array")
        raw_broll = []
    for index, item in enumerate(raw_broll or []):
        if not isinstance(item, dict):
            parse_errors.append(f"broll_plan[{index}] must be an object")
            continue
        overreach.extend(f"broll_plan[{index}].{key}" for key in sorted(set(item) - _BROLL_FIELDS))
        for field_name in sorted(_BROLL_FIELDS - set(item)):
            parse_errors.append(f"broll_plan[{index}] missing field '{field_name}'")
        slot_id = _strict_string(item.get("slot_id"), f"broll_plan[{index}].slot_id", parse_errors)
        candidate_id = _strict_string(
            item.get("candidate_id"), f"broll_plan[{index}].candidate_id", parse_errors
        )
        reason = _strict_string(
            item.get("reason"), f"broll_plan[{index}].reason", parse_errors, allow_empty=True
        )
        if not slot_id or not candidate_id or reason is None:
            continue
        broll.append(
            BrollChoice(
                slot_id=slot_id,
                candidate_id=candidate_id,
                reason=reason,
            )
        )
    analysis = _strict_string(data.get("analysis"), "analysis", parse_errors, allow_empty=True)
    return MediaSelection(
        portrait=portrait,
        broll=broll,
        analysis=analysis or "",
        overreach_fields=tuple(dict.fromkeys(overreach)),
        parse_errors=tuple(dict.fromkeys(parse_errors)),
    )


def _strict_string(
    value: Any,
    path: str,
    errors: list[str],
    *,
    allow_empty: bool = False,
) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{path} must be a string")
        return None
    text = value.strip()
    if not text and not allow_empty:
        errors.append(f"{path} must be non-empty")
        return None
    return text


def index_media_candidates(material: dict) -> MediaCandidates:
    portrait = [
        item
        for item in (material.get("portrait_candidates") or [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    broll = [
        item
        for item in (material.get("broll_candidates") or [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    return MediaCandidates(
        portrait_by_id={f"pc_{index:03d}": item for index, item in enumerate(portrait)},
        broll_by_id={f"bc_{index:03d}": item for index, item in enumerate(broll)},
    )


def _source_frames_available(candidate: dict) -> int:
    clean_span = longest_clean_portrait_source_span(_meta(candidate))
    if clean_span is None:
        return 0
    return max(0, frame_index(clean_span[1]) - frame_index(clean_span[0]))


def _broll_source_frames_available(candidate: dict) -> int:
    meta = _meta(candidate)
    return max(
        0,
        frame_index(_as_float(meta.get("source_end")))
        - frame_index(_as_float(meta.get("source_start"))),
    )


def _required_frames(slot: dict) -> int:
    if slot.get("source_length_frames") is not None:
        return max(0, int(slot.get("source_length_frames", 0) or 0))
    return max(
        0,
        int(slot.get("end_frame", 0) or 0) - int(slot.get("start_frame", 0) or 0),
    )


def _topk(slot: dict, retrieval: dict[str, list[str]] | None) -> list[str]:
    if retrieval is None:
        return []
    return [
        _as_str(value)
        for value in retrieval.get(_as_str(slot.get("slot_id")), [])
        if _as_str(value)
    ]


def _has_retrieval_constraint(slot: dict, retrieval: dict[str, list[str]] | None) -> bool:
    return retrieval is not None and _as_str(slot.get("slot_id")) in retrieval


def build_media_agent_input(
    *,
    request,
    boundary: dict,
    candidates: MediaCandidates,
    narration_units: list[dict],
    duration: float,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
) -> dict:
    portrait_slots = []
    for slot in boundary.get("portrait_slots") or []:
        if not isinstance(slot, dict):
            continue
        need = _required_frames(slot)
        payload = {
            **slot,
            "required_frames": need,
            "required_seconds": round(to_seconds(need), 3),
            "legal_candidate_ids": [
                candidate_id
                for candidate_id, candidate in candidates.portrait_by_id.items()
                if _source_frames_available(candidate) >= need
            ],
        }
        if _has_retrieval_constraint(slot, retrieval_topk_by_window):
            payload["retrieval_topk_candidate_ids"] = _topk(slot, retrieval_topk_by_window)
        portrait_slots.append(payload)
    broll_slots = []
    for slot in boundary.get("broll_slots") or []:
        if not isinstance(slot, dict):
            continue
        need = _required_frames(slot)
        payload = {
            **slot,
            "required_frames": need,
            "required_seconds": round(to_seconds(need), 3),
            "multi_clip_allowed": False,
        }
        if _has_retrieval_constraint(slot, retrieval_topk_by_window):
            payload["retrieval_topk_candidate_ids"] = _topk(slot, retrieval_topk_by_window)
        broll_slots.append(payload)
    full_coverage = request.broll.enabled and request.broll.mode == "full_coverage"
    return {
        "script": request.script,
        "title": request.title or "",
        "edit_instruction": request.edit.instruction,
        "video_duration": round(float(duration), 3),
        "max_broll_inserts": len(broll_slots)
        if full_coverage
        else request.broll.max_inserts
        if request.broll.enabled
        else 0,
        "portrait_uniqueness_rule": PORTRAIT_UNIQUENESS_RULE,
        "broll_uniqueness_rule": (
            BROLL_FULL_COVERAGE_UNIQUENESS_RULE
            if full_coverage
            else BROLL_INSERT_UNIQUENESS_RULE
        ),
        "narration_units": [
            {
                "unit_id": _as_str(unit.get("unit_id")),
                "text": _as_str(unit.get("text")),
                "start": _as_float(unit.get("start")),
                "end": _as_float(unit.get("end")),
            }
            for unit in narration_units
            if isinstance(unit, dict)
        ],
        "safe_cut_boundaries": [
            {
                "cut_id": _as_str(item.get("cut_id")),
                "frame": int(item.get("frame", 0) or 0),
                "source": _as_str(item.get("source")),
            }
            for item in boundary.get("safe_cut_boundaries") or []
            if isinstance(item, dict)
        ],
        "portrait_slots": portrait_slots,
        "broll_slots": broll_slots,
        "portrait_candidates": [
            {
                "candidate_id": candidate_id,
                "asset_id": _as_str(candidate.get("asset_id")),
                "available_seconds": round(to_seconds(_source_frames_available(candidate)), 3),
                "description": _as_str(_meta(candidate).get("description")),
                "reason": _as_str(candidate.get("reason")),
            }
            for candidate_id, candidate in candidates.portrait_by_id.items()
        ],
        "broll_candidates": [
            {
                "candidate_id": candidate_id,
                "asset_id": _as_str(candidate.get("asset_id")),
                "available_seconds": round(
                    to_seconds(_broll_source_frames_available(candidate)), 3
                ),
                "scene_name": _as_str(_meta(candidate).get("scene_name")),
                "diversity_key": _as_str(_meta(candidate).get("diversity_key")),
                "matched_keywords": list(_meta(candidate).get("matched_keywords") or []),
                "description": _as_str(_meta(candidate).get("description")),
            }
            for candidate_id, candidate in candidates.broll_by_id.items()
        ],
    }


def validate_media_selection(
    selection: MediaSelection,
    *,
    boundary: dict,
    candidates: MediaCandidates,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
    allow_broll_asset_diversity_reuse: bool = False,
    require_broll_coverage: bool = False,
) -> list[str]:
    errors: list[str] = []
    errors.extend(selection.parse_errors)
    if selection.overreach_fields:
        errors.append(
            "media selection includes fields outside the exact schema: "
            + ", ".join(selection.overreach_fields)
        )
    portrait_slots = {
        _as_str(slot.get("slot_id")): slot
        for slot in boundary.get("portrait_slots") or []
        if isinstance(slot, dict)
    }
    seen_slots: set[str] = set()
    used_assets: dict[str, str] = {}
    for choice in selection.portrait:
        slot = portrait_slots.get(choice.slot_id)
        if slot is None:
            errors.append(f"portrait slot_id '{choice.slot_id}' is unknown")
            continue
        if choice.slot_id in seen_slots:
            errors.append(f"portrait slot '{choice.slot_id}' is assigned more than once")
            continue
        seen_slots.add(choice.slot_id)
        candidate = candidates.portrait_by_id.get(choice.candidate_id)
        if candidate is None:
            errors.append(f"portrait candidate_id '{choice.candidate_id}' is unknown")
            continue
        topk = set(_topk(slot, retrieval_topk_by_window))
        if (
            _has_retrieval_constraint(slot, retrieval_topk_by_window)
            and choice.candidate_id not in topk
        ):
            errors.append(
                f"portrait candidate_id '{choice.candidate_id}' is not legal for slot "
                f"'{choice.slot_id}'"
            )
        if _source_frames_available(candidate) < _required_frames(slot):
            errors.append(
                f"portrait candidate '{choice.candidate_id}' is too short for slot "
                f"'{choice.slot_id}'"
            )
        asset_id = _as_str(candidate.get("asset_id"))
        if asset_id in used_assets:
            errors.append(
                f"portrait asset_id '{asset_id}' is assigned to both "
                f"'{used_assets[asset_id]}' and '{choice.slot_id}'"
            )
        elif asset_id:
            used_assets[asset_id] = choice.slot_id
    missing = sorted(set(portrait_slots) - seen_slots)
    if missing:
        errors.append(f"portrait slots not covered: {', '.join(missing)}")

    broll_slots = {
        _as_str(slot.get("slot_id")): slot
        for slot in boundary.get("broll_slots") or []
        if isinstance(slot, dict)
    }
    seen_broll_slots: set[str] = set()
    used_candidates: set[str] = set()
    used_broll_assets: set[str] = set()
    used_diversity: set[str] = set()
    for choice in selection.broll:
        slot = broll_slots.get(choice.slot_id)
        if slot is None:
            errors.append(f"broll slot_id '{choice.slot_id}' is unknown")
            continue
        if choice.slot_id in seen_broll_slots:
            errors.append(f"broll slot '{choice.slot_id}' is assigned more than once")
            continue
        seen_broll_slots.add(choice.slot_id)
        candidate = candidates.broll_by_id.get(choice.candidate_id)
        if candidate is None:
            errors.append(f"broll candidate_id '{choice.candidate_id}' is unknown")
            continue
        topk = set(_topk(slot, retrieval_topk_by_window))
        if (
            _has_retrieval_constraint(slot, retrieval_topk_by_window)
            and choice.candidate_id not in topk
        ):
            errors.append(
                f"broll candidate_id '{choice.candidate_id}' is not legal for slot "
                f"'{choice.slot_id}'"
            )
        if choice.candidate_id in used_candidates:
            errors.append(f"broll candidate_id '{choice.candidate_id}' is assigned more than once")
        used_candidates.add(choice.candidate_id)
        if _broll_source_frames_available(candidate) < _required_frames(slot):
            errors.append(
                f"broll candidate '{choice.candidate_id}' is too short for slot '{choice.slot_id}'"
            )
        if not allow_broll_asset_diversity_reuse:
            asset_id = _as_str(candidate.get("asset_id"))
            diversity = _as_str(_meta(candidate).get("diversity_key"))
            if asset_id and asset_id in used_broll_assets:
                errors.append(f"broll asset_id '{asset_id}' is assigned more than once")
            if diversity and diversity in used_diversity:
                errors.append(f"broll diversity_key '{diversity}' is assigned more than once")
            used_broll_assets.add(asset_id)
            if diversity:
                used_diversity.add(diversity)
    missing_broll = sorted(set(broll_slots) - seen_broll_slots)
    if require_broll_coverage and missing_broll:
        errors.append(f"broll slots not covered: {', '.join(missing_broll)}")
    return errors


def deterministic_media_selection(
    *,
    boundary: dict,
    candidates: MediaCandidates,
    max_inserts: int,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
    allow_broll_asset_diversity_reuse: bool = False,
) -> MediaSelection:
    ranked_portrait = sorted(
        candidates.portrait_by_id,
        key=lambda candidate_id: (
            -_as_float(candidates.portrait_by_id[candidate_id].get("score")),
            candidate_id,
        ),
    )
    portrait: list[PortraitChoice] = []
    used_portrait_assets: set[str] = set()
    for slot in boundary.get("portrait_slots") or []:
        if not isinstance(slot, dict):
            continue
        pool = (
            _topk(slot, retrieval_topk_by_window)
            if _has_retrieval_constraint(slot, retrieval_topk_by_window)
            else ranked_portrait
        )
        candidate_id = next(
            (
                value
                for value in pool
                if value in candidates.portrait_by_id
                and _source_frames_available(candidates.portrait_by_id[value])
                >= _required_frames(slot)
                and _as_str(candidates.portrait_by_id[value].get("asset_id"))
                not in used_portrait_assets
            ),
            None,
        )
        if candidate_id is None:
            continue
        asset_id = _as_str(candidates.portrait_by_id[candidate_id].get("asset_id"))
        if asset_id:
            used_portrait_assets.add(asset_id)
        portrait.append(
            PortraitChoice(
                slot_id=_as_str(slot.get("slot_id")),
                candidate_id=candidate_id,
                reason="deterministic top-score",
            )
        )

    ranked_broll = sorted(
        candidates.broll_by_id,
        key=lambda candidate_id: (
            -_as_float(candidates.broll_by_id[candidate_id].get("score")),
            candidate_id,
        ),
    )
    broll: list[BrollChoice] = []
    used_candidates: set[str] = set()
    used_assets: set[str] = set()
    used_diversity: set[str] = set()
    for slot in boundary.get("broll_slots") or []:
        if len(broll) >= max(0, max_inserts) or not isinstance(slot, dict):
            break
        pool = (
            _topk(slot, retrieval_topk_by_window)
            if _has_retrieval_constraint(slot, retrieval_topk_by_window)
            else ranked_broll
        )
        for candidate_id in pool:
            candidate = candidates.broll_by_id.get(candidate_id)
            if candidate is None or candidate_id in used_candidates:
                continue
            asset_id = _as_str(candidate.get("asset_id"))
            diversity = _as_str(_meta(candidate).get("diversity_key"))
            if (
                _broll_source_frames_available(candidate) < _required_frames(slot)
                or not allow_broll_asset_diversity_reuse
                and (asset_id in used_assets or diversity and diversity in used_diversity)
            ):
                continue
            used_candidates.add(candidate_id)
            if asset_id:
                used_assets.add(asset_id)
            if diversity:
                used_diversity.add(diversity)
            broll.append(
                BrollChoice(
                    slot_id=_as_str(slot.get("slot_id")),
                    candidate_id=candidate_id,
                    reason="deterministic coverage",
                )
            )
            break
    return MediaSelection(
        portrait=portrait,
        broll=broll,
        analysis="deterministic fallback selection",
    )


def repair_media_selection_to_constraints(
    *,
    selection: MediaSelection,
    boundary: dict,
    candidates: MediaCandidates,
    max_inserts: int,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
    allow_broll_asset_diversity_reuse: bool = False,
    require_broll_coverage: bool = False,
) -> tuple[MediaSelection, list[dict], list[str]]:
    """Repair only local media constraints, never an invalid provider schema."""

    if selection.parse_errors or selection.overreach_fields:
        return (
            selection,
            [],
            validate_media_selection(
                selection,
                boundary=boundary,
                candidates=candidates,
                retrieval_topk_by_window=retrieval_topk_by_window,
                allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
                require_broll_coverage=require_broll_coverage,
            ),
        )

    repaired_portrait, portrait_actions = _repair_portrait_choices(
        selection=selection,
        boundary=boundary,
        candidates=candidates,
        retrieval_topk_by_window=retrieval_topk_by_window,
    )
    repaired = MediaSelection(
        portrait=repaired_portrait,
        broll=selection.broll,
        analysis=selection.analysis,
    )
    repaired_broll, broll_actions = _repair_broll_choices(
        selection=repaired,
        boundary=boundary,
        candidates=candidates,
        max_inserts=max_inserts,
        retrieval_topk_by_window=retrieval_topk_by_window,
        allow_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
        require_broll_coverage=require_broll_coverage,
    )
    repaired = MediaSelection(
        portrait=repaired_portrait,
        broll=repaired_broll,
        analysis=selection.analysis,
    )
    errors = validate_media_selection(
        repaired,
        boundary=boundary,
        candidates=candidates,
        retrieval_topk_by_window=retrieval_topk_by_window,
        allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
        require_broll_coverage=require_broll_coverage,
    )
    return repaired, [*portrait_actions, *broll_actions], errors


def _repair_portrait_choices(
    *,
    selection: MediaSelection,
    boundary: dict,
    candidates: MediaCandidates,
    retrieval_topk_by_window: dict[str, list[str]] | None,
) -> tuple[list[PortraitChoice], list[dict]]:
    slots = {
        _as_str(slot.get("slot_id")): slot
        for slot in boundary.get("portrait_slots") or []
        if isinstance(slot, dict) and _as_str(slot.get("slot_id"))
    }
    used_slots: set[str] = set()
    used_assets: set[str] = set()
    repaired: list[PortraitChoice] = []
    actions: list[dict] = []

    def _asset_id(candidate_id: str) -> str:
        candidate = candidates.portrait_by_id.get(candidate_id)
        return _as_str(candidate.get("asset_id")) if candidate is not None else ""

    def _usable(candidate_id: str, slot: dict) -> bool:
        candidate = candidates.portrait_by_id.get(candidate_id)
        if candidate is None:
            return False
        if _has_retrieval_constraint(slot, retrieval_topk_by_window):
            if candidate_id not in set(_topk(slot, retrieval_topk_by_window)):
                return False
        if _source_frames_available(candidate) < _required_frames(slot):
            return False
        asset_id = _asset_id(candidate_id)
        return not asset_id or asset_id not in used_assets

    ranked_ids = sorted(
        candidates.portrait_by_id,
        key=lambda candidate_id: (
            -_as_float(candidates.portrait_by_id[candidate_id].get("score")),
            candidate_id,
        ),
    )
    for choice in selection.portrait:
        slot = slots.get(choice.slot_id)
        if slot is None or choice.slot_id in used_slots:
            repaired.append(choice)
            continue
        if _usable(choice.candidate_id, slot):
            replacement_id = choice.candidate_id
        else:
            pool = (
                _topk(slot, retrieval_topk_by_window)
                if _has_retrieval_constraint(slot, retrieval_topk_by_window)
                else ranked_ids
            )
            replacement_id = next(
                (candidate_id for candidate_id in pool if _usable(candidate_id, slot)),
                "",
            )
        if not replacement_id:
            repaired.append(choice)
            continue
        used_slots.add(choice.slot_id)
        asset_id = _asset_id(replacement_id)
        if asset_id:
            used_assets.add(asset_id)
        repaired.append(
            PortraitChoice(
                slot_id=choice.slot_id,
                candidate_id=replacement_id,
                reason=choice.reason
                if replacement_id == choice.candidate_id
                else f"{choice.reason}（本地约束修正：{choice.candidate_id} -> {replacement_id}）",
            )
        )
        if replacement_id != choice.candidate_id:
            actions.append(
                {
                    "kind": "portrait",
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "repaired_candidate_id": replacement_id,
                    "action": "replaced",
                }
            )
    return repaired, actions


def _repair_broll_choices(
    *,
    selection: MediaSelection,
    boundary: dict,
    candidates: MediaCandidates,
    max_inserts: int,
    retrieval_topk_by_window: dict[str, list[str]] | None,
    allow_asset_diversity_reuse: bool,
    require_broll_coverage: bool,
) -> tuple[list[BrollChoice], list[dict]]:
    slots = {
        _as_str(slot.get("slot_id")): slot
        for slot in boundary.get("broll_slots") or []
        if isinstance(slot, dict) and _as_str(slot.get("slot_id"))
    }
    used_slots: set[str] = set()
    used_candidates: set[str] = set()
    used_assets: set[str] = set()
    used_diversity: set[str] = set()
    repaired: list[BrollChoice] = []
    actions: list[dict] = []

    def _usable(candidate_id: str, slot: dict) -> bool:
        candidate = candidates.broll_by_id.get(candidate_id)
        if candidate is None or candidate_id in used_candidates:
            return False
        if _has_retrieval_constraint(slot, retrieval_topk_by_window):
            if candidate_id not in set(_topk(slot, retrieval_topk_by_window)):
                return False
        if _broll_source_frames_available(candidate) < _required_frames(slot):
            return False
        if allow_asset_diversity_reuse:
            return True
        asset_id = _as_str(candidate.get("asset_id"))
        diversity = _as_str(_meta(candidate).get("diversity_key"))
        return not (asset_id and asset_id in used_assets) and not (
            diversity and diversity in used_diversity
        )

    def _reserve(candidate_id: str) -> None:
        candidate = candidates.broll_by_id[candidate_id]
        used_candidates.add(candidate_id)
        asset_id = _as_str(candidate.get("asset_id"))
        diversity = _as_str(_meta(candidate).get("diversity_key"))
        if asset_id:
            used_assets.add(asset_id)
        if diversity:
            used_diversity.add(diversity)

    ranked_ids = sorted(
        candidates.broll_by_id,
        key=lambda candidate_id: (
            -_as_float(candidates.broll_by_id[candidate_id].get("score")),
            candidate_id,
        ),
    )

    def _pool(slot: dict, preferred_id: str = "") -> list[str]:
        values = (
            _topk(slot, retrieval_topk_by_window)
            if _has_retrieval_constraint(slot, retrieval_topk_by_window)
            else ranked_ids
        )
        return sorted(values, key=lambda candidate_id: candidate_id != preferred_id)

    for choice in selection.broll:
        if len(repaired) >= max(0, max_inserts):
            actions.append(
                {
                    "kind": "broll",
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "action": "dropped",
                    "reason": "max_broll_inserts reached",
                }
            )
            continue
        slot = slots.get(choice.slot_id)
        if slot is None or choice.slot_id in used_slots:
            actions.append(
                {
                    "kind": "broll",
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "action": "dropped",
                    "reason": "unknown or duplicate slot",
                }
            )
            continue
        replacement_id = next(
            (
                candidate_id
                for candidate_id in _pool(slot, choice.candidate_id)
                if _usable(candidate_id, slot)
            ),
            "",
        )
        used_slots.add(choice.slot_id)
        if not replacement_id:
            actions.append(
                {
                    "kind": "broll",
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "action": "dropped",
                    "reason": "no legal broll candidate remains",
                }
            )
            continue
        _reserve(replacement_id)
        repaired.append(
            BrollChoice(
                slot_id=choice.slot_id,
                candidate_id=replacement_id,
                reason=choice.reason
                if replacement_id == choice.candidate_id
                else f"{choice.reason}（本地约束修正：{choice.candidate_id} -> {replacement_id}）",
            )
        )
        if replacement_id != choice.candidate_id:
            actions.append(
                {
                    "kind": "broll",
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "repaired_candidate_id": replacement_id,
                    "action": "replaced",
                }
            )

    if require_broll_coverage:
        for slot_id, slot in slots.items():
            if slot_id in used_slots or len(repaired) >= max(0, max_inserts):
                continue
            candidate_id = next(
                (candidate_id for candidate_id in _pool(slot) if _usable(candidate_id, slot)),
                "",
            )
            if not candidate_id:
                continue
            _reserve(candidate_id)
            used_slots.add(slot_id)
            repaired.append(
                BrollChoice(
                    slot_id=slot_id,
                    candidate_id=candidate_id,
                    reason="本地 full_coverage 补窗",
                )
            )
            actions.append(
                {
                    "kind": "broll",
                    "slot_id": slot_id,
                    "repaired_candidate_id": candidate_id,
                    "action": "filled",
                    "reason": "filled full_coverage window with one legal candidate",
                }
            )
    return repaired, actions


def select_media_with_repair(
    *,
    invoke: Callable[[list[str]], Any],
    boundary: dict,
    candidates: MediaCandidates,
    max_inserts: int,
    max_repair_attempts: int,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
    allow_broll_asset_diversity_reuse: bool = False,
    require_broll_coverage: bool = False,
) -> tuple[MediaSelection, list[dict], list[str]]:
    """Select media, repairing deterministic constraints before another provider call.

    A provider re-prompt is reserved for schema errors or constraints that the local
    candidate pool cannot repair. This keeps a valid first response usable when the
    only defect is an ID/duration/diversity conflict, and avoids exposing that result
    to a needless second remote call.
    """

    errors: list[str] = []
    trace: list[dict] = []
    selection = MediaSelection()
    for attempt in range(max(0, max_repair_attempts) + 1):
        selection = parse_media_selection(invoke(errors))
        errors = validate_media_selection(
            selection,
            boundary=boundary,
            candidates=candidates,
            retrieval_topk_by_window=retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
            require_broll_coverage=require_broll_coverage,
        )
        trace.append({"attempt": attempt, "error_count": len(errors), "errors": errors})
        if not errors:
            break
        repaired, actions, repaired_errors = repair_media_selection_to_constraints(
            selection=selection,
            boundary=boundary,
            candidates=candidates,
            max_inserts=max_inserts,
            retrieval_topk_by_window=retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
            require_broll_coverage=require_broll_coverage,
        )
        if actions:
            trace.append(
                {
                    "attempt": "local_media_constraint_repair",
                    "provider_attempt": attempt,
                    "error_count": len(repaired_errors),
                    "errors": repaired_errors,
                    "actions": actions,
                }
            )
        selection = repaired
        errors = repaired_errors
        if not errors:
            break
    return selection, trace, errors
