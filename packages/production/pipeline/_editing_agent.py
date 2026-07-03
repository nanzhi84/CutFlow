"""Pure helpers for the LLM ``EditingAgentPlanning`` node (issue #136).

The editing agent lets an LLM make the *semantic* editing choices (which
portrait source window fills each boundary slot, which b-roll clip covers which
narration beat, which font, which BGM) while every frame-exact boundary is
computed locally by the deterministic frame-grid primitives. The LLM therefore
only ever emits candidate IDs — never authoritative frame numbers — so a
hallucinated timeline can never reach the renderer.

This module is import-light and free of any ``NodeContext``/IO so the selection
parsing, validation, deterministic fallback and the three materializers can be
unit-tested as pure functions. The node
(``nodes.editing_agent_planning``) wires them to the provider gateway + prompt
registry and repairs an invalid selection before falling back.

Materializers reuse the SAME primitives the deterministic nodes use:
``frame_grid.slice_source_window`` for portrait source frames and
``broll_plan.align_insertions_to_portrait_cuts`` for b-roll cut-snapping, so the
emitted ``plan.portrait`` / ``plan.broll`` / ``plan.style`` artifacts are
byte-for-byte the same shape the deterministic pipeline produces and the
downstream ``TimelinePlanning`` verify-only path needs no special casing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from packages.core.contracts.artifacts import (
    BgmPlan,
    BrollOverlay,
    BrollPlanArtifact,
    FontPlan,
    OverlayEvent,
    PortraitPlanArtifact,
    PortraitSegment,
    StylePlanArtifact,
    SubtitleStylePlan,
)
from packages.planning.editing.frame_grid import (
    FrameWindow,
    frame_index,
    slice_source_window,
    to_seconds,
)
from packages.planning.material.broll_plan import (
    BROLL_GEOMETRY_POLICY,
    BrollGeometryPolicy,
    BrollInsertion,
    place_insertion_safely,
)

TIMELINE_FPS = 30


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
    start = _as_float(meta.get("source_start"))
    end = _as_float(meta.get("source_end"))
    if end <= start:
        return 0
    return frame_index(end) - frame_index(start)


def _slot_required_frames(slot: dict) -> int:
    return max(0, int(slot.get("end_frame", 0)) - int(slot.get("start_frame", 0)))


def _legal_portrait_window_ids(slot: dict, candidates: IndexedCandidates) -> list[str]:
    need = _slot_required_frames(slot)
    return [
        cid
        for cid, cand in candidates.portrait_by_id.items()
        if _source_frames_available(cand) >= need
    ]


def _portrait_asset_key(candidate: dict) -> str:
    return _as_str(candidate.get("asset_id"))


def _distinct_portrait_asset_count(candidates: IndexedCandidates) -> int:
    return len(
        {key for cand in candidates.portrait_by_id.values() if (key := _portrait_asset_key(cand))}
    )


def portrait_asset_reuse_cap(*, boundary: dict, candidates: IndexedCandidates) -> int:
    """Max portrait slots a single source asset may fill for this run.

    Strict uniqueness (cap ``1``) is the hardened default. When the distinct
    portrait asset pool ``A`` is smaller than the slot count ``S`` — a common
    shortage when an operator uploads only 2-3 portrait sources for an 8-15 slot
    short video — strict uniqueness is unsatisfiable and would strand slots, so
    the cap relaxes to ``ceil(S / A)``: a deterministic, balanced reuse budget
    whose total capacity ``A * ceil(S / A) >= S`` always covers every slot while
    still spreading coverage across the available sources.
    """
    assets = _distinct_portrait_asset_count(candidates)
    slots = sum(1 for s in (boundary.get("portrait_slots") or []) if isinstance(s, dict))
    if assets <= 0 or slots <= 0 or assets >= slots:
        return 1
    return -(-slots // assets)


def portrait_uniqueness_rule_text(cap: int) -> str:
    """Human-readable portrait uniqueness rule, kept in lock-step with the cap.

    The LLM prompt embeds this sentence so the model's constraint matches exactly
    what ``validate_selection`` enforces — a strict one-slot rule when assets are
    plentiful, or the relaxed per-asset reuse budget when they are scarce.
    """
    if cap <= 1:
        return (
            "同一个 asset_id 最多只能用于一个 portrait_slot"
            "（人像素材充足，禁止在多个 slot 复用同一素材）。"
        )
    return (
        f"人像可用素材数量少于人像插槽数，允许复用：同一个 asset_id 最多可用于 {cap} 个 "
        "portrait_slot，请尽量把覆盖均衡分散到不同素材上，不要把所有 slot 都堆到同一个素材。"
    )


def build_agent_input(
    *,
    request,
    boundary: dict,
    candidates: IndexedCandidates,
    narration_units: list[dict],
    duration: float,
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
        portrait_slots.append(
            {
                **slot,
                "required_frames": need,
                "required_seconds": round(to_seconds(need), 3),
                "legal_window_ids": _legal_portrait_window_ids(slot, candidates),
            }
        )

    return {
        "script": request.script,
        "title": request.title or "",
        "edit_instruction": request.edit.instruction,
        "video_duration": round(float(duration), 3),
        "max_broll_inserts": request.broll.max_inserts if request.broll.enabled else 0,
        "portrait_uniqueness_rule": portrait_uniqueness_rule_text(
            portrait_asset_reuse_cap(boundary=boundary, candidates=candidates)
        ),
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
        "broll_slots": boundary.get("broll_slots") or [],
        "portrait_candidates": [
            {
                "candidate_id": cid,
                "asset_id": _as_str(cand.get("asset_id")),
                "clip_id": _as_str(_meta(cand).get("clip_id")),
                "source_start": _as_float(_meta(cand).get("source_start")),
                "source_end": _as_float(_meta(cand).get("source_end")),
                "available_frames": _source_frames_available(cand),
                "available_seconds": round(to_seconds(_source_frames_available(cand)), 3),
                "score": _as_float(cand.get("score")),
                "reason": _as_str(cand.get("reason")),
            }
            for cid, cand in candidates.portrait_by_id.items()
        ],
        "broll_candidates": [
            {
                "candidate_id": cid,
                "asset_id": _as_str(cand.get("asset_id")),
                "clip_id": _as_str(_meta(cand).get("clip_id")),
                "source_start": _as_float(_meta(cand).get("source_start")),
                "source_end": _as_float(_meta(cand).get("source_end")),
                "matched_keywords": _meta(cand).get("matched_keywords") or [],
                "scene_name": _as_str(_meta(cand).get("scene_name")),
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
    # each source asset used at most ``asset_use_cap`` times (1 when assets are
    # plentiful, ceil(S/A) when scarce — see portrait_asset_reuse_cap).
    asset_use_cap = portrait_asset_reuse_cap(boundary=boundary, candidates=candidates)
    seen_slots: set[str] = set()
    asset_slots: dict[str, list[str]] = {}
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
            prior_slots = asset_slots.setdefault(asset_key, [])
            if len(prior_slots) >= asset_use_cap:
                if asset_use_cap == 1:
                    errors.append(
                        f"portrait asset_id '{asset_key}' is assigned to more than one slot "
                        f"('{prior_slots[0]}' and '{choice.slot_id}'); choose a different asset"
                    )
                else:
                    errors.append(
                        f"portrait asset_id '{asset_key}' is reused more than the allowed "
                        f"{asset_use_cap} slots (portrait assets are scarce); spread coverage "
                        f"across the other portrait assets"
                    )
            prior_slots.append(choice.slot_id)
    missing = sorted(set(portrait_slots) - seen_slots)
    if missing:
        errors.append(f"portrait slots not covered: {', '.join(missing)}")

    # B-roll: valid, unique, in-bounds slot + candidate; slots never overlap by
    # construction (one per narration unit) so uniqueness is the only overlap gate.
    seen_broll: set[str] = set()
    for choice in selection.broll:
        if choice.slot_id not in broll_slots:
            errors.append(f"broll slot_id '{choice.slot_id}' is not a known broll slot")
            continue
        if choice.slot_id in seen_broll:
            errors.append(f"broll slot '{choice.slot_id}' is covered more than once")
            continue
        seen_broll.add(choice.slot_id)
        if choice.candidate_id not in candidates.broll_by_id:
            errors.append(f"broll candidate_id '{choice.candidate_id}' is not a known candidate")

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
) -> EditingSelection:
    """Score-ranked default selection equivalent to the deterministic nodes.

    Used when there is no real LLM provider (sandbox). Portrait picks honour the
    per-asset reuse budget from ``portrait_asset_reuse_cap`` (strict one-slot when
    assets are plentiful, balanced ceil(S/A) reuse when scarce) and always fill
    every slot — the portrait main track must stay gap-free, so a slot is never
    dropped: the cap is relaxed, and as a last resort the top-ranked source is
    reused (materialize_portrait clone-pads a short source) rather than leaving a
    hole.
    """
    portrait_slots = [s for s in (boundary.get("portrait_slots") or []) if isinstance(s, dict)]
    broll_slots = [s for s in (boundary.get("broll_slots") or []) if isinstance(s, dict)]
    ranked_portrait = _ranked_ids(candidates.portrait_by_id)
    ranked_broll = _ranked_ids(candidates.broll_by_id)
    ranked_font = _ranked_ids(candidates.font_by_id)
    ranked_bgm = _ranked_ids(candidates.bgm_by_id)

    reuse_cap = portrait_asset_reuse_cap(boundary=boundary, candidates=candidates)
    portrait: list[PortraitChoice] = []
    asset_use: dict[str, int] = {}
    for slot in portrait_slots:
        need = _slot_required_frames(slot)
        # Prefer a long-enough source whose asset still has reuse budget.
        window_id = next(
            (
                cid
                for cid in ranked_portrait
                if _source_frames_available(candidates.portrait_by_id[cid]) >= need
                and asset_use.get(_portrait_asset_key(candidates.portrait_by_id[cid]), 0) < reuse_cap
            ),
            None,
        )
        if window_id is None:
            # Budget exhausted across every long-enough asset: relax the cap
            # rather than strand the slot.
            window_id = next(
                (
                    cid
                    for cid in ranked_portrait
                    if _source_frames_available(candidates.portrait_by_id[cid]) >= need
                ),
                None,
            )
        if window_id is None and ranked_portrait:
            # No source is long enough at all: reuse the top-ranked one (clone-pad
            # fills the remainder) — still a covered slot, never a hole.
            window_id = ranked_portrait[0]
        if window_id is None:
            continue
        asset_key = _portrait_asset_key(candidates.portrait_by_id[window_id])
        if asset_key:
            asset_use[asset_key] = asset_use.get(asset_key, 0) + 1
        portrait.append(
            PortraitChoice(
                slot_id=_as_str(slot.get("slot_id")),
                window_id=window_id,
                reason="deterministic top-score",
            )
        )

    broll: list[BrollChoice] = []
    if ranked_broll and max_inserts > 0:
        for slot in broll_slots[:max_inserts]:
            broll.append(
                BrollChoice(
                    slot_id=_as_str(slot.get("slot_id")),
                    candidate_id=ranked_broll[0],
                    reason="deterministic coverage",
                    confidence=0.5,
                )
            )
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
            selection, boundary=boundary, candidates=candidates, bgm_enabled=bgm_enabled
        )
        trace.append({"attempt": attempt, "error_count": len(errors), "errors": errors})
        if not errors:
            break
    return selection, trace, errors


# --------------------------------------------------------------------------- #
# Materializers: ID selection -> frame-exact artifacts
# --------------------------------------------------------------------------- #
def materialize_portrait(
    *,
    selection: EditingSelection,
    boundary: dict,
    candidates: IndexedCandidates,
) -> dict:
    """Turn portrait ID choices into a frame-exact ``PortraitPlanArtifact``.

    Each slot's timeline window is authoritative (from #135's frame-aligned
    portrait_slots); the chosen candidate only supplies the source clip, whose
    frames come from ``slice_source_window`` — never from the LLM.
    """
    choice_by_slot = {c.slot_id: c for c in selection.portrait}
    slots = sorted(
        (s for s in (boundary.get("portrait_slots") or []) if isinstance(s, dict)),
        key=lambda s: int(s.get("start_frame", 0)),
    )
    segments: list[PortraitSegment] = []
    for index, slot in enumerate(slots):
        choice = choice_by_slot.get(_as_str(slot.get("slot_id")))
        if choice is None:
            continue
        cand = candidates.portrait_by_id.get(choice.window_id)
        if cand is None:
            continue
        meta = _meta(cand)
        window = FrameWindow(
            start_frame=int(slot.get("start_frame", 0)),
            end_frame=int(slot.get("end_frame", 0)),
        )
        src_start = _as_float(meta.get("source_start"))
        src_end = _as_float(meta.get("source_end"))
        source_window, _pad_end = slice_source_window(
            source_start_seconds=src_start,
            length_frames=window.length_frames,
            source_window_start_seconds=src_start,
            source_window_end_seconds=src_end if src_end > src_start else None,
        )
        segments.append(
            PortraitSegment(
                segment_id=f"portrait_{index + 1}",
                asset_id=_as_str(cand.get("asset_id")) or None,
                clip_id=_as_str(meta.get("clip_id")) or None,
                start_sec=to_seconds(window.start_frame),
                end_sec=to_seconds(window.end_frame),
                source_start=to_seconds(source_window.start_frame),
                source_end=to_seconds(source_window.end_frame),
                role="main",
                source_mode=choice.source_mode or "lipsynced",
                boundary_source=_as_str(slot.get("boundary_source")) or None,
                boundary_reason=None,
                unit_ids=[_as_str(u) for u in (slot.get("unit_ids") or [])],
                slot_phase="portrait_opening" if index == 0 else "portrait_main",
                recently_used_material=False,
                timeline_start_frame=window.start_frame,
                timeline_end_frame=window.end_frame,
                source_start_frame=source_window.start_frame,
                source_end_frame=source_window.end_frame,
            )
        )
    total_frames = segments[-1].timeline_end_frame if segments else 0
    total_duration = round(to_seconds(total_frames), 3)
    return PortraitPlanArtifact(
        fps=TIMELINE_FPS,
        total_duration=total_duration,
        asset_id=segments[0].asset_id if segments else None,
        duration_sec=total_duration,
        segments=segments,
        diagnostics={"planner": "editing_agent", "segment_count": len(segments)},
    ).model_dump(mode="json")


def portrait_cut_frames(portrait_payload: dict) -> list[int]:
    return sorted(
        {
            int(frame)
            for seg in portrait_payload.get("segments", [])
            for frame in (seg.get("timeline_start_frame"), seg.get("timeline_end_frame"))
            if frame is not None
        }
    )


def materialize_broll(
    *,
    selection: EditingSelection,
    boundary: dict,
    candidates: IndexedCandidates,
    cut_frames: list[int],
    enabled: bool,
    max_inserts: int,
    policy: BrollGeometryPolicy = BROLL_GEOMETRY_POLICY,
) -> tuple[dict, list[dict]]:
    """Turn b-roll ID choices into a frame-aligned ``BrollPlanArtifact``.

    Each chosen slot+candidate becomes a ``BrollInsertion`` whose timeline span
    is the #135 broll slot (clamped to the clip's available source) and whose
    frames + clone-pad come from the SAME ``align_insertions_to_portrait_cuts``
    the deterministic BrollPlanning uses, so overlays snap to portrait cuts and
    never overlap.
    """
    if not enabled:
        return BrollPlanArtifact(enabled=False).model_dump(mode="json"), []
    slot_by_id = {
        _as_str(s.get("slot_id")): s
        for s in (boundary.get("broll_slots") or [])
        if isinstance(s, dict)
    }
    accepted: list[BrollInsertion] = []
    drop_diagnostics: list[dict] = []
    for choice in selection.broll[: max(0, max_inserts)]:
        slot = slot_by_id.get(choice.slot_id)
        cand = candidates.broll_by_id.get(choice.candidate_id)
        drop_base = {"slot_id": choice.slot_id, "candidate_id": choice.candidate_id}
        if slot is None:
            drop_diagnostics.append({**drop_base, "reason": "unknown_slot"})
            continue
        if cand is None:
            drop_diagnostics.append({**drop_base, "reason": "unknown_candidate"})
            continue
        meta = _meta(cand)
        ts = to_seconds(int(slot.get("start_frame", 0)))
        te = to_seconds(int(slot.get("end_frame", 0)))
        src_start = _as_float(meta.get("source_start"))
        src_end = _as_float(meta.get("source_end"))
        slot_span = max(0.0, te - ts)
        source_available = src_end - src_start
        if 0.0 < source_available < policy.min_insert_seconds:
            drop_diagnostics.append({**drop_base, "reason": "source_too_short"})
            continue
        source_limit = source_available if source_available > 0.0 else policy.max_insert_seconds
        span = min(policy.max_insert_seconds, slot_span, source_limit)
        if span < policy.min_insert_seconds:
            drop_diagnostics.append({**drop_base, "reason": "slot_too_short"})
            continue
        insert = BrollInsertion(
            asset_id=_as_str(cand.get("asset_id")),
            clip_id=_as_str(meta.get("clip_id")),
            timeline_start=ts,
            timeline_end=round(ts + span, 3),
            source_start=src_start,
            source_end=round(src_start + span, 3),
            confidence=choice.confidence,
            matched_keywords=choice.matched_keywords,
            scene_name=_as_str(meta.get("scene_name")),
            reason=choice.reason or "editing agent selection",
            diversity_key=_as_str(meta.get("diversity_key")),
        )
        next_accepted = place_insertion_safely(
            accepted,
            insert,
            window_start=ts,
            window_end=te,
            fps=TIMELINE_FPS,
            portrait_cut_frames=cut_frames,
            policy=policy,
        )
        if next_accepted is None:
            drop_diagnostics.append({**drop_base, "reason": "geometry_rejected"})
            continue
        accepted = next_accepted
    overlays = [
        BrollOverlay(
            overlay_id=f"broll_{index + 1}",
            asset_id=ins.asset_id,
            clip_id=ins.clip_id or None,
            timeline_start=ins.timeline_start,
            timeline_end=ins.timeline_end,
            source_start=ins.source_start,
            source_end=ins.source_end,
            timeline_start_frame=ins.timeline_start_frame,
            timeline_end_frame=ins.timeline_end_frame,
            source_start_frame=ins.source_start_frame,
            source_end_frame=ins.source_end_frame,
            pad_start=ins.pad_start,
            pad_end=ins.pad_end,
            reason=ins.reason,
            confidence=ins.confidence,
            matched_keywords=list(ins.matched_keywords),
            scene_name=ins.scene_name or None,
            diversity_key=ins.diversity_key or None,
        )
        for index, ins in enumerate(accepted)
    ]
    payload = BrollPlanArtifact(enabled=True, overlays=overlays).model_dump(mode="json")
    return payload, drop_diagnostics


def materialize_style(
    *,
    selection: EditingSelection,
    candidates: IndexedCandidates,
    request,
    overlay_events: list[OverlayEvent],
) -> dict:
    """Turn font/BGM ID choices into a ``StylePlanArtifact``.

    Subtitle base style stays request-driven (mirrors StylePlanning); the LLM's
    font choice sets the authoritative ``font_asset_id`` (default sentinel when
    absent/invalid); BGM is filled from the chosen candidate's annotation.
    """
    font_id = selection.font_id
    if font_id and font_id in candidates.font_by_id:
        font_asset_id = font_id
    elif candidates.font_by_id:
        font_asset_id = next(iter(candidates.font_by_id))
    else:
        font_asset_id = "case_default_font"

    bgm_plan: BgmPlan | None = None
    bgm_asset_id: str | None = None
    if request.bgm.enabled and selection.bgm_id and selection.bgm_id in candidates.bgm_by_id:
        cand = candidates.bgm_by_id[selection.bgm_id]
        meta = _meta(cand)
        bgm_asset_id = _as_str(cand.get("asset_id"))
        bgm_plan = BgmPlan(
            enabled=True,
            asset_id=bgm_asset_id,
            segment_id=_as_str(meta.get("clip_id")) or None,
            source_start=_as_float(meta.get("source_start"))
            if meta.get("source_start") is not None
            else None,
            source_end=_as_float(meta.get("source_end"))
            if meta.get("source_end") is not None
            else None,
            duration=_as_float(meta.get("duration")) if meta.get("duration") is not None else None,
            section_type=_as_str(meta.get("section_type")),
            section_label=_as_str(meta.get("section_label")),
            repeat_group=_as_str(meta.get("repeat_group")),
            loopable=bool(meta.get("loopable")),
            energy_profile=_as_str(meta.get("energy_profile")),
            mood=_as_str(meta.get("mood")),
            scene_fit=[_as_str(x) for x in (meta.get("scene_fit") or []) if _as_str(x)],
            script_fit=[_as_str(x) for x in (meta.get("script_fit") or []) if _as_str(x)],
            avoid_script=[_as_str(x) for x in (meta.get("avoid_script") or []) if _as_str(x)],
            reason=_as_str(meta.get("reason")) or _as_str(cand.get("reason")),
            volume=request.bgm.volume,
            auto_mix=request.bgm.auto_mix,
        )

    return StylePlanArtifact(
        subtitle=SubtitleStylePlan(
            font_id=request.subtitle.font_id,
            font_size=request.subtitle.font_size,
            position=request.subtitle.position,
        ),
        bgm=bgm_plan,
        font=FontPlan(font_id=font_asset_id),
        font_asset_id=font_asset_id,
        bgm_asset_id=bgm_asset_id,
        overlay_events=overlay_events,
    ).model_dump(mode="json")
