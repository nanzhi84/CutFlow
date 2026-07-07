"""Pure helpers for the LLM ``EditingAgentPlanning`` node (issue #136).

The editing agent lets an LLM make the *semantic* editing choices (which
portrait source window fills each boundary slot, which b-roll clip covers which
narration beat, which font, which BGM) while every frame-exact boundary is
computed locally by the deterministic frame-grid primitives. The LLM therefore
only ever emits candidate IDs — never authoritative frame numbers — so a
hallucinated timeline can never reach the renderer.

This module is import-light and free of any ``NodeContext``/IO so the selection
parsing, validation, and deterministic fallback can be unit-tested as pure
functions. Frame-exact artifact construction lives in ``_materialize`` so the
agent and deterministic nodes share the same materialization helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from packages.planning.editing.frame_grid import (
    frame_index,
    to_seconds,
)
from packages.planning.material import longest_clean_portrait_source_span

TIMELINE_FPS = 30
PORTRAIT_UNIQUENESS_RULE = (
    "同一个 asset_id 最多只能用于一个 portrait_slot"
    "（人像切镜窗口由 TimelineWindowPlanning 按 strict uniqueness 编译，Agent 禁止放宽复用）。"
)


# --------------------------------------------------------------------------- #
# Selection data structures + LLM-output parsing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PortraitChoice:
    slot_id: str
    window_id: str
    source_mode: str = "lipsynced"
    reason: str = ""


@dataclass(frozen=True)
class BrollChoice:
    slot_id: str
    candidate_id: str
    reason: str = ""
    confidence: float = 0.0
    matched_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditingSelection:
    portrait: list[PortraitChoice] = field(default_factory=list)
    broll: list[BrollChoice] = field(default_factory=list)
    font_id: str | None = None
    bgm_id: str | None = None
    analysis: str = ""


def _as_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_selection(output: Any) -> EditingSelection:
    """Best-effort parse of the LLM JSON into a typed selection.

    Never raises on a malformed blob: missing/garbage keys degrade to empty
    lists / ``None`` so the local validator (not a parse crash) is the single
    place an invalid selection is rejected and repaired.
    """
    data = output if isinstance(output, dict) else {}
    portrait: list[PortraitChoice] = []
    for item in data.get("portrait_plan") or []:
        if not isinstance(item, dict):
            continue
        slot_id = _as_str(item.get("slot_id"))
        window_id = _as_str(item.get("window_id") or item.get("candidate_id"))
        if not slot_id or not window_id:
            continue
        portrait.append(
            PortraitChoice(
                slot_id=slot_id,
                window_id=window_id,
                source_mode=_as_str(item.get("source_mode")) or "lipsynced",
                reason=_as_str(item.get("reason")),
            )
        )
    broll: list[BrollChoice] = []
    for item in data.get("broll_plan") or []:
        if not isinstance(item, dict):
            continue
        slot_id = _as_str(item.get("slot_id"))
        candidate_id = _as_str(item.get("candidate_id") or item.get("window_id"))
        if not slot_id or not candidate_id:
            continue
        raw_kw = item.get("matched_keywords")
        keywords = (
            tuple(_as_str(kw) for kw in raw_kw if _as_str(kw)) if isinstance(raw_kw, list) else ()
        )
        broll.append(
            BrollChoice(
                slot_id=slot_id,
                candidate_id=candidate_id,
                reason=_as_str(item.get("reason")),
                confidence=_as_float(item.get("confidence")),
                matched_keywords=keywords,
            )
        )
    font_plan = data.get("font_plan") if isinstance(data.get("font_plan"), dict) else {}
    bgm_plan = data.get("bgm_plan") if isinstance(data.get("bgm_plan"), dict) else {}
    return EditingSelection(
        portrait=portrait,
        broll=broll,
        font_id=_as_str(font_plan.get("font_id")) or None,
        bgm_id=_as_str(bgm_plan.get("bgm_id")) or None,
        analysis=_as_str(data.get("analysis")),
    )


# --------------------------------------------------------------------------- #
# Candidate indexing + LLM input assembly
# --------------------------------------------------------------------------- #
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
    """Assign stable IDs to every material-pack candidate.

    Portrait/b-roll candidates are index-keyed (``pc_000`` / ``bc_000``) because
    one asset can appear as several clip-level source windows; font/BGM are
    asset-keyed since they are one-per-asset. The LLM references these IDs and
    the materializers resolve them back — the LLM never sees a raw frame.
    """
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


def _meta(candidate: dict) -> dict:
    meta = candidate.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _source_frames_available(candidate: dict) -> int:
    meta = _meta(candidate)
    clean_span = longest_clean_portrait_source_span(meta)
    if clean_span is None:
        return 0
    start, end = clean_span
    return frame_index(end) - frame_index(start)


def _broll_source_frames_available(candidate: dict) -> int:
    meta = _meta(candidate)
    start = _as_float(meta.get("source_start"))
    end = _as_float(meta.get("source_end"))
    return max(0, frame_index(end) - frame_index(start))


def _clean_source_span_for_payload(candidate: dict) -> tuple[float, float]:
    meta = _meta(candidate)
    clean_span = longest_clean_portrait_source_span(meta)
    if clean_span is not None:
        return clean_span
    source_start = _as_float(meta.get("source_start"))
    return source_start, source_start


def _portrait_candidate_payload(cid: str, cand: dict) -> dict:
    clean_start, clean_end = _clean_source_span_for_payload(cand)
    available_frames = frame_index(clean_end) - frame_index(clean_start)
    return {
        "candidate_id": cid,
        "asset_id": _as_str(cand.get("asset_id")),
        "clip_id": _as_str(_meta(cand).get("clip_id")),
        "source_start": clean_start,
        "source_end": clean_end,
        "available_frames": available_frames,
        "available_seconds": round(to_seconds(available_frames), 3),
        "description": _as_str(_meta(cand).get("description")),
        "score": _as_float(cand.get("score")),
        "reason": _as_str(cand.get("reason")),
    }


def _slot_required_frames(slot: dict) -> int:
    if slot.get("source_length_frames") is not None:
        return max(0, int(slot.get("source_length_frames", 0) or 0))
    return max(0, int(slot.get("end_frame", 0)) - int(slot.get("start_frame", 0)))


def _legal_portrait_window_ids(slot: dict, candidates: IndexedCandidates) -> list[str]:
    need = _slot_required_frames(slot)
    return [
        cid
        for cid, cand in candidates.portrait_by_id.items()
        if _source_frames_available(cand) >= need
    ]


def _topk_for_slot(slot: dict, retrieval_topk_by_window: dict[str, list[str]] | None) -> list[str]:
    if not retrieval_topk_by_window:
        return []
    return [
        _as_str(candidate_id)
        for candidate_id in retrieval_topk_by_window.get(_as_str(slot.get("slot_id")), [])
        if _as_str(candidate_id)
    ]


def _slot_has_retrieval_constraint(
    slot: dict, retrieval_topk_by_window: dict[str, list[str]] | None
) -> bool:
    if retrieval_topk_by_window is None:
        return False
    return _as_str(slot.get("slot_id")) in retrieval_topk_by_window


def _portrait_asset_key(candidate: dict) -> str:
    return _as_str(candidate.get("asset_id"))


def build_agent_input(
    *,
    request,
    boundary: dict,
    candidates: IndexedCandidates,
    narration_units: list[dict],
    duration: float,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
) -> dict:
    """Assemble the numbered, frame-free structure handed to the LLM.

    Everything the agent needs to make semantic choices — the narration beats,
    the safe cut boundaries + slots #135 already quantized, and the ID-tagged
    candidate pools with their semantic annotations — and nothing it must not
    invent. Slot frame windows are input-only constraints; the LLM still emits
    IDs only, never authoritative frame values.
    """
    portrait_slots = []
    for slot in (boundary.get("portrait_slots") or []):
        if not isinstance(slot, dict):
            continue
        need = _slot_required_frames(slot)
        payload = {
            **slot,
            "required_frames": need,
            "required_seconds": round(to_seconds(need), 3),
            "legal_window_ids": _legal_portrait_window_ids(slot, candidates),
        }
        if _slot_has_retrieval_constraint(slot, retrieval_topk_by_window):
            payload["retrieval_topk_candidate_ids"] = _topk_for_slot(
                slot,
                retrieval_topk_by_window,
            )
        portrait_slots.append(payload)

    broll_slots = []
    for slot in (boundary.get("broll_slots") or []):
        if not isinstance(slot, dict):
            continue
        need = _slot_required_frames(slot)
        payload = {
            **slot,
            "required_frames": need,
            "required_seconds": round(to_seconds(need), 3),
        }
        if _slot_has_retrieval_constraint(slot, retrieval_topk_by_window):
            payload["retrieval_topk_candidate_ids"] = _topk_for_slot(
                slot,
                retrieval_topk_by_window,
            )
        broll_slots.append(payload)

    max_broll_inserts = request.broll.max_inserts if request.broll.enabled else 0
    if request.broll.enabled and getattr(request.broll, "mode", "insert") == "full_coverage":
        max_broll_inserts = len(broll_slots)

    return {
        "script": request.script,
        "title": request.title or "",
        "edit_instruction": request.edit.instruction,
        "video_duration": round(float(duration), 3),
        "max_broll_inserts": max_broll_inserts,
        "portrait_uniqueness_rule": PORTRAIT_UNIQUENESS_RULE,
        "narration_units": [
            {
                "unit_id": _as_str(u.get("unit_id")),
                "text": _as_str(u.get("text")),
                "start": _as_float(u.get("start")),
                "end": _as_float(u.get("end")),
                "pause_after_ms": int(_as_float(u.get("pause_after_ms"))),
                "portrait_cut_allowed": bool(u.get("portrait_cut_allowed")),
                "boundary_score": _as_float(u.get("boundary_score")),
                "boundary_reason": _as_str(u.get("boundary_reason")),
            }
            for u in narration_units
        ],
        "safe_cut_boundaries": boundary.get("safe_cut_boundaries") or [],
        "portrait_slots": portrait_slots,
        "broll_slots": broll_slots,
        "portrait_candidates": [
            _portrait_candidate_payload(cid, cand)
            for cid, cand in candidates.portrait_by_id.items()
        ],
        "broll_candidates": [
            {
                "candidate_id": cid,
                "asset_id": _as_str(cand.get("asset_id")),
                "clip_id": _as_str(_meta(cand).get("clip_id")),
                "source_start": _as_float(_meta(cand).get("source_start")),
                "source_end": _as_float(_meta(cand).get("source_end")),
                "available_frames": _broll_source_frames_available(cand),
                "available_seconds": round(to_seconds(_broll_source_frames_available(cand)), 3),
                "matched_keywords": _meta(cand).get("matched_keywords") or [],
                "scene_name": _as_str(_meta(cand).get("scene_name")),
                "description": _as_str(_meta(cand).get("description")),
                "score": _as_float(cand.get("score")),
            }
            for cid, cand in candidates.broll_by_id.items()
        ],
        "font_candidates": [
            {
                "font_id": cid,
                "score": _as_float(cand.get("score")),
                "reason": _as_str(cand.get("reason")),
            }
            for cid, cand in candidates.font_by_id.items()
        ],
        "bgm_candidates": [
            {
                "bgm_id": cid,
                "mood": _as_str(_meta(cand).get("mood")),
                "energy_profile": _as_str(_meta(cand).get("energy_profile")),
                "script_fit": _meta(cand).get("script_fit") or [],
                "scene_fit": _meta(cand).get("scene_fit") or [],
                "score": _as_float(cand.get("score")),
            }
            for cid, cand in candidates.bgm_by_id.items()
        ],
    }


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_selection(
    selection: EditingSelection,
    *,
    boundary: dict,
    candidates: IndexedCandidates,
    bgm_enabled: bool,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
    allow_broll_asset_diversity_reuse: bool = False,
) -> list[str]:
    """Local hard constraints on the LLM's ID-only selection.

    Returns a list of human-readable error strings (empty == valid). These are
    fed back verbatim on the repair prompt so the model can correct itself.
    """
    errors: list[str] = []
    portrait_slots = {
        _as_str(s.get("slot_id")): s
        for s in (boundary.get("portrait_slots") or [])
        if isinstance(s, dict)
    }
    broll_slots = {
        _as_str(s.get("slot_id")): s
        for s in (boundary.get("broll_slots") or [])
        if isinstance(s, dict)
    }

    # Portrait: every slot covered exactly once by a valid, long-enough window;
    # each source asset is used at most once. Scarcity is resolved upstream by
    # TimelineWindowPlanning, not by an agent-local relaxation.
    seen_slots: set[str] = set()
    asset_slots: dict[str, str] = {}
    for choice in selection.portrait:
        if choice.slot_id not in portrait_slots:
            errors.append(f"portrait slot_id '{choice.slot_id}' is not a known portrait slot")
            continue
        if choice.slot_id in seen_slots:
            errors.append(f"portrait slot '{choice.slot_id}' is assigned more than once")
            continue
        seen_slots.add(choice.slot_id)
        cand = candidates.portrait_by_id.get(choice.window_id)
        if cand is None:
            errors.append(f"portrait window_id '{choice.window_id}' is not a known candidate")
            continue
        retrieval_topk = set(_topk_for_slot(portrait_slots[choice.slot_id], retrieval_topk_by_window))
        if (
            _slot_has_retrieval_constraint(portrait_slots[choice.slot_id], retrieval_topk_by_window)
            and choice.window_id not in retrieval_topk
        ):
            legal_hint = ", ".join(sorted(retrieval_topk)[:20]) if retrieval_topk else "none"
            errors.append(
                f"portrait window_id '{choice.window_id}' is not in retrieval_topk_candidate_ids "
                f"for slot '{choice.slot_id}'; choose one of: {legal_hint}"
            )
        slot = portrait_slots[choice.slot_id]
        need = _slot_required_frames(slot)
        available = _source_frames_available(cand)
        if available < need:
            legal = _legal_portrait_window_ids(slot, candidates)
            legal_hint = ", ".join(legal[:20]) if legal else "none"
            errors.append(
                f"portrait window '{choice.window_id}' source is too short: has {available} frames "
                f"but slot '{choice.slot_id}' requires {need} frames; "
                f"choose one of legal_window_ids: {legal_hint}"
            )
        asset_key = _portrait_asset_key(cand)
        if asset_key:
            prior_slot = asset_slots.get(asset_key)
            if prior_slot is not None:
                errors.append(
                    f"portrait asset_id '{asset_key}' is assigned to more than one slot "
                    f"('{prior_slot}' and '{choice.slot_id}'); choose a different asset"
                )
            else:
                asset_slots[asset_key] = choice.slot_id
    missing = sorted(set(portrait_slots) - seen_slots)
    if missing:
        errors.append(f"portrait slots not covered: {', '.join(missing)}")

    # B-roll: valid optional window + unique candidate. Insert mode also keeps
    # asset/diversity unique; full coverage may reuse long assets across windows.
    seen_broll: set[str] = set()
    seen_broll_candidates: set[str] = set()
    broll_asset_slots: dict[str, str] = {}
    broll_diversity_slots: dict[str, str] = {}
    broll_covered_frames_by_slot: dict[str, int] = {}
    for choice in selection.broll:
        if choice.slot_id not in broll_slots:
            errors.append(f"broll slot_id '{choice.slot_id}' is not a known broll slot")
            continue
        if not allow_broll_asset_diversity_reuse and choice.slot_id in seen_broll:
            errors.append(f"broll slot '{choice.slot_id}' is covered more than once")
            continue
        seen_broll.add(choice.slot_id)
        cand = candidates.broll_by_id.get(choice.candidate_id)
        if cand is None:
            errors.append(f"broll candidate_id '{choice.candidate_id}' is not a known candidate")
            continue
        retrieval_topk = set(_topk_for_slot(broll_slots[choice.slot_id], retrieval_topk_by_window))
        if (
            _slot_has_retrieval_constraint(broll_slots[choice.slot_id], retrieval_topk_by_window)
            and choice.candidate_id not in retrieval_topk
        ):
            legal_hint = ", ".join(sorted(retrieval_topk)[:20]) if retrieval_topk else "none"
            errors.append(
                f"broll candidate_id '{choice.candidate_id}' is not in retrieval_topk_candidate_ids "
                f"for slot '{choice.slot_id}'; choose one of: {legal_hint}"
            )
        if choice.candidate_id in seen_broll_candidates:
            errors.append(f"broll candidate_id '{choice.candidate_id}' is assigned more than once")
            continue
        seen_broll_candidates.add(choice.candidate_id)
        slot = broll_slots[choice.slot_id]
        need = _slot_required_frames(slot)
        available = _broll_source_frames_available(cand)
        if allow_broll_asset_diversity_reuse:
            broll_covered_frames_by_slot[choice.slot_id] = (
                broll_covered_frames_by_slot.get(choice.slot_id, 0) + max(0, available)
            )
        elif available < need:
            errors.append(
                f"broll candidate '{choice.candidate_id}' source is too short: has "
                f"{available} frames but slot '{choice.slot_id}' requires {need} frames"
            )
        if not allow_broll_asset_diversity_reuse:
            asset_key = _as_str(cand.get("asset_id"))
            if asset_key:
                prior_slot = broll_asset_slots.get(asset_key)
                if prior_slot is not None:
                    errors.append(
                        f"broll asset_id '{asset_key}' is assigned to more than one slot "
                        f"('{prior_slot}' and '{choice.slot_id}'); choose a different asset"
                    )
                else:
                    broll_asset_slots[asset_key] = choice.slot_id
            diversity_key = _as_str(_meta(cand).get("diversity_key"))
            if diversity_key:
                prior_slot = broll_diversity_slots.get(diversity_key)
                if prior_slot is not None:
                    errors.append(
                        f"broll diversity_key '{diversity_key}' is assigned to more than one slot "
                        f"('{prior_slot}' and '{choice.slot_id}'); choose a different scene cluster"
                    )
                else:
                    broll_diversity_slots[diversity_key] = choice.slot_id

    if allow_broll_asset_diversity_reuse:
        missing_broll = []
        for slot_id, slot in broll_slots.items():
            need = _slot_required_frames(slot)
            covered = broll_covered_frames_by_slot.get(slot_id, 0)
            if covered < need:
                missing_broll.append(f"{slot_id} ({covered}/{need} frames)")
        if missing_broll:
            errors.append("broll slots not fully covered: " + ", ".join(sorted(missing_broll)))

    # Font / BGM: an explicit choice must reference a real candidate; null is fine
    # (empty candidate pool → default font / no BGM).
    if selection.font_id is not None and selection.font_id not in candidates.font_by_id:
        errors.append(f"font_id '{selection.font_id}' is not a known font candidate")
    if (
        bgm_enabled
        and selection.bgm_id is not None
        and selection.bgm_id not in candidates.bgm_by_id
    ):
        errors.append(f"bgm_id '{selection.bgm_id}' is not a known bgm candidate")
    return errors


# --------------------------------------------------------------------------- #
# Deterministic fallback (sandbox / no real provider / unrepairable)
# --------------------------------------------------------------------------- #
def _ranked_ids(by_id: dict[str, dict]) -> list[str]:
    return [
        cid
        for cid, _ in sorted(by_id.items(), key=lambda kv: (-_as_float(kv[1].get("score")), kv[0]))
    ]


def deterministic_selection(
    *,
    boundary: dict,
    candidates: IndexedCandidates,
    bgm_enabled: bool,
    max_inserts: int,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
    allow_broll_asset_diversity_reuse: bool = False,
) -> EditingSelection:
    """Score-ranked default selection equivalent to the deterministic nodes.

    Used when the agent falls back to local selection for b-roll/font/BGM. Portrait
    choices are strict-unique best effort only; the node's fallback portrait track
    comes from ``TimelineWindowPlanning.default_assignment`` instead.
    """
    portrait_slots = [s for s in (boundary.get("portrait_slots") or []) if isinstance(s, dict)]
    broll_slots = [s for s in (boundary.get("broll_slots") or []) if isinstance(s, dict)]
    ranked_portrait = _ranked_ids(candidates.portrait_by_id)
    ranked_broll = _ranked_ids(candidates.broll_by_id)
    ranked_font = _ranked_ids(candidates.font_by_id)
    ranked_bgm = _ranked_ids(candidates.bgm_by_id)

    portrait: list[PortraitChoice] = []
    used_assets: set[str] = set()
    for slot in portrait_slots:
        need = _slot_required_frames(slot)
        if _slot_has_retrieval_constraint(slot, retrieval_topk_by_window):
            portrait_pool = _topk_for_slot(slot, retrieval_topk_by_window)
        else:
            portrait_pool = ranked_portrait
        window_id = next(
            (
                cid
                for cid in portrait_pool
                if cid in candidates.portrait_by_id
                if _source_frames_available(candidates.portrait_by_id[cid]) >= need
                and _portrait_asset_key(candidates.portrait_by_id[cid]) not in used_assets
            ),
            None,
        )
        if window_id is None:
            continue
        asset_key = _portrait_asset_key(candidates.portrait_by_id[window_id])
        if asset_key:
            used_assets.add(asset_key)
        portrait.append(
            PortraitChoice(
                slot_id=_as_str(slot.get("slot_id")),
                window_id=window_id,
                reason="deterministic top-score",
            )
        )

    broll: list[BrollChoice] = []
    used_broll_candidates: set[str] = set()
    used_broll_assets: set[str] = set()
    used_broll_diversity: set[str] = set()
    if ranked_broll and max_inserts > 0:
        for slot in broll_slots:
            if len(broll) >= max(0, max_inserts):
                break
            need = _slot_required_frames(slot)
            if _slot_has_retrieval_constraint(slot, retrieval_topk_by_window):
                broll_pool = _topk_for_slot(slot, retrieval_topk_by_window)
            else:
                broll_pool = ranked_broll
            covered = 0
            for candidate_id in broll_pool:
                if len(broll) >= max(0, max_inserts):
                    break
                if candidate_id not in candidates.broll_by_id:
                    continue
                candidate = candidates.broll_by_id[candidate_id]
                source_frames = _broll_source_frames_available(candidate)
                if (
                    candidate_id in used_broll_candidates
                    or source_frames <= 0
                    or (not allow_broll_asset_diversity_reuse and source_frames < need)
                    or (
                        not allow_broll_asset_diversity_reuse
                        and _as_str(candidate.get("asset_id")) in used_broll_assets
                    )
                    or (
                        not allow_broll_asset_diversity_reuse
                        and _as_str(_meta(candidate).get("diversity_key"))
                        and _as_str(_meta(candidate).get("diversity_key")) in used_broll_diversity
                    )
                ):
                    continue
                used_broll_candidates.add(candidate_id)
                asset_key = _as_str(candidate.get("asset_id"))
                if asset_key:
                    used_broll_assets.add(asset_key)
                diversity_key = _as_str(_meta(candidate).get("diversity_key"))
                if diversity_key:
                    used_broll_diversity.add(diversity_key)
                broll.append(
                    BrollChoice(
                        slot_id=_as_str(slot.get("slot_id")),
                        candidate_id=candidate_id,
                        reason="deterministic coverage",
                        confidence=0.5,
                    )
                )
                covered += source_frames
                if not allow_broll_asset_diversity_reuse or covered >= need:
                    break
    return EditingSelection(
        portrait=portrait,
        broll=broll,
        font_id=ranked_font[0] if ranked_font else None,
        bgm_id=ranked_bgm[0] if (bgm_enabled and ranked_bgm) else None,
        analysis="deterministic fallback selection",
    )


# --------------------------------------------------------------------------- #
# LLM selection + local repair loop
# --------------------------------------------------------------------------- #
def select_with_repair(
    *,
    invoke: Callable[[list[str]], Any],
    boundary: dict,
    candidates: IndexedCandidates,
    bgm_enabled: bool,
    max_repair_attempts: int,
    retrieval_topk_by_window: dict[str, list[str]] | None = None,
    allow_broll_asset_diversity_reuse: bool = False,
) -> tuple[EditingSelection, list[dict], list[str]]:
    """Drive one LLM selection + up to ``max_repair_attempts`` local repairs.

    ``invoke(previous_errors)`` performs the actual render+provider call and
    returns the raw LLM output (IO lives in the caller's closure so this loop
    stays pure and unit-testable). The parsed selection is validated locally;
    on failure the validator's error strings are fed back into the next invoke.
    Returns ``(selection, trace, errors)`` — a non-empty ``errors`` means the
    selection is still invalid after the last attempt and the caller decides
    whether to fail-fast (real provider) or fall back (sandbox).
    """
    errors: list[str] = []
    trace: list[dict] = []
    selection = EditingSelection()
    for attempt in range(max(0, max_repair_attempts) + 1):
        output = invoke(errors)
        selection = parse_selection(output)
        errors = validate_selection(
            selection,
            boundary=boundary,
            candidates=candidates,
            bgm_enabled=bgm_enabled,
            retrieval_topk_by_window=retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
        )
        trace.append({"attempt": attempt, "error_count": len(errors), "errors": errors})
        if not errors:
            break
    return selection, trace, errors
