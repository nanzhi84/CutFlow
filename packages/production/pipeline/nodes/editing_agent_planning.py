"""EditingAgentPlanning node: one LLM综合剪辑 pass -> portrait/broll/style plans.

Replaces the deterministic portrait / B-roll / style planning stages with a
single LLM node for the ``digital_human_editing_agent_v1`` template (issue #136).
The LLM only makes semantic ID choices; the local materializers (``_editing_agent``)
turn them into
the SAME frame-exact ``plan.portrait`` / ``plan.broll`` / ``plan.style``
artifacts the deterministic nodes emit, so ``TimelinePlanning`` and the whole
render chain are untouched.

Selection flow:
  * real ``llm.chat`` provider  -> render + invoke + parse + local validate,
    repairing up to ``request.edit.max_repair_attempts`` times; still invalid ->
    fail-fast (``prompt.output_invalid``).
  * no real provider (sandbox)  -> deterministic score-ranked fallback, reported
    as a graded degradation (never a silent downgrade). Production with the
    sandbox gate off fail-fasts on the missing provider instead.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from packages.ai.gateway import ProviderCall
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DegradationNotice,
    ErrorCode,
    NodeStatus,
    WarningCode,
    utcnow,
)
from packages.core.contracts.artifacts import MediaAssignmentPlan
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing.frame_grid import frame_index
from packages.planning.material import longest_clean_portrait_source_span, shortlist_for_windows
from packages.production.pipeline._editing_agent import (
    BrollChoice,
    EditingSelection,
    IndexedCandidates,
    PortraitChoice,
    _slot_has_retrieval_constraint as _agent_slot_has_retrieval_constraint,
    _slot_required_frames as _agent_slot_required_frames,
    _source_frames_available as _agent_source_frames_available,
    _topk_for_slot as _agent_topk_for_slot,
    build_agent_input,
    deterministic_selection,
    index_candidates,
    select_with_repair,
    validate_selection,
)
from packages.production.pipeline._caption_styles import (
    HUAZI_ANIMATION_DIRECTIONS,
    HUAZI_ANIMATIONS,
)
from packages.production.pipeline._huazi_candidates import (
    HuaziPlanChoice,
    derive_huazi_candidates,
    finalize_huazi_plan,
    normal_caption_top_y,
    parse_huazi_plan,
    validate_huazi_plan,
)
from packages.production.pipeline._huazi_layout import generate_layout_boxes
from packages.production.pipeline._materialize import (
    _subtitle_font_size,
    _subtitle_position,
    full_coverage_broll_coverage_gaps,
    materialize_broll_from_assignment,
    materialize_full_coverage_broll_from_assignment,
    materialize_portrait_from_assignment,
    materialize_style_from_selection,
    portrait_cut_frames,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline.nodes._broll_policy import broll_full_coverage_enabled
from packages.production.pipeline.nodes._creative_intent import load_creative_intent

# Structured variables serialized as JSON for the prompt; scalars go through str().
_JSON_VARS = frozenset(
    {
        "narration_units",
        "safe_cut_boundaries",
        "portrait_slots",
        "broll_slots",
        "bgm_candidates",
    }
)
# HuaziPlanningSubagent variables serialized as JSON (structured); rest go str().
_HUAZI_JSON_VARS = frozenset(
    {
        "candidate_events",
        "layout_boxes",
        "track_summary",
        "animation_candidates",
        "animation_directions",
    }
)
# The huazi subagent gets a single self-correction pass (spec 2.5): one repair.
_HUAZI_MAX_REPAIR_ATTEMPTS = 1
_PROMPT_RETRIEVAL_TOPK_LIMIT = 12
_PROMPT_BGM_CANDIDATE_LIMIT = 6
_PORTRAIT_CANDIDATE_HEADER = (
    "candidate_id | asset_id | available_seconds | description | reason"
)
_BROLL_CANDIDATE_HEADER = (
    "candidate_id | asset_id | scene_name | allowed_slot_ids | matched_keywords | "
    "available_seconds | description"
)


@dataclass(frozen=True)
class EditingAgentContext:
    material: dict
    narration: dict
    boundary: dict
    windows: dict
    raw_units: list[dict]
    duration: float
    agent_boundary: dict
    shortlisted_material: dict
    shortlist_counts: dict
    candidates: IndexedCandidates
    retrieval_topk_by_window: dict[str, list[str]]
    huazi_candidates: list[dict]
    agent_input: dict


@dataclass(frozen=True)
class EditingAgentSelectionResult:
    selection: EditingSelection
    engine: str
    fallback_used: bool
    fallback_reason: str | None
    repair_trace: list[dict]
    warnings: list[WarningCode]
    degradations: list[DegradationNotice]
    provider_invocation_ids: list[str]


@dataclass(frozen=True)
class EditingAgentMaterializedOutputs:
    assignment_payload: dict
    portrait_payload: dict
    broll_payload: dict
    style_payload: dict
    diagnostics: dict
    warnings: list[WarningCode]
    degradations: list[DegradationNotice]


@dataclass(frozen=True)
class HuaziPlanningResult:
    overlay_events: list  # list[OverlayEvent]
    warnings: list[WarningCode]
    degradations: list[DegradationNotice]
    provider_invocation_ids: list[str]
    diagnostics: dict


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _prompt_variables(agent_input: dict, previous_errors: list[str]) -> dict:
    variables = {
        key: (json.dumps(value, ensure_ascii=False) if key in _JSON_VARS else str(value))
        for key, value in agent_input.items()
    }
    variables["repair_feedback"] = (
        "上一轮选择存在以下问题，请只修正这些点后重新只输出 JSON：\n- "
        + "\n- ".join(previous_errors)
        if previous_errors
        else ""
    )
    return variables


def _portrait_feasibility_failure(agent_input: dict) -> dict | None:
    portrait_slots = [s for s in agent_input.get("portrait_slots", []) if isinstance(s, dict)]
    failed_slot_ids = [
        str(slot.get("slot_id") or "")
        for slot in portrait_slots
        if _portrait_slot_has_no_legal_choice(slot) and str(slot.get("slot_id") or "")
    ]
    if not failed_slot_ids:
        return None

    required_frames_by_slot = {
        str(slot.get("slot_id")): int(slot.get("required_frames", 0) or 0)
        for slot in portrait_slots
        if str(slot.get("slot_id") or "") in failed_slot_ids
    }
    portrait_candidates = [
        c for c in agent_input.get("portrait_candidates", []) if isinstance(c, dict)
    ]
    longest_available_source_frames = max(
        [int(c.get("available_frames", 0) or 0) for c in portrait_candidates] or [0]
    )
    return {
        "failed_slot_ids": failed_slot_ids,
        "required_frames_by_slot": required_frames_by_slot,
        "longest_available_source_frames": longest_available_source_frames,
        "portrait_candidate_count": len(portrait_candidates),
    }


def _portrait_slot_has_no_legal_choice(slot: dict) -> bool:
    legal_window_ids = {str(item) for item in (slot.get("legal_window_ids") or []) if str(item)}
    if "retrieval_topk_candidate_ids" not in slot:
        return not legal_window_ids
    retrieval_topk = {
        str(item) for item in (slot.get("retrieval_topk_candidate_ids") or []) if str(item)
    }
    return not (legal_window_ids & retrieval_topk)


def _boundary_with_compiled_windows(boundary: dict, windows: dict) -> dict:
    compiled = dict(boundary)
    compiled["portrait_slots"] = [
        _portrait_slot_from_window(window)
        for window in (windows.get("portrait_windows") or [])
        if isinstance(window, dict)
    ]
    compiled["broll_slots"] = [
        _broll_slot_from_window(window)
        for window in (windows.get("broll_windows") or [])
        if isinstance(window, dict)
    ]
    return compiled


def _raw_portrait_candidate_diagnostics(material: dict) -> dict:
    candidates = [
        item
        for item in (material.get("portrait_candidates") or [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    return {
        "longest_available_source_frames": max(
            [_source_frames_available(candidate) for candidate in candidates] or [0]
        ),
        "portrait_candidate_count": len(candidates),
    }


def _source_frames_available(candidate: dict) -> int:
    meta = candidate.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    clean_span = longest_clean_portrait_source_span(meta)
    if clean_span is None:
        return 0
    start, end = clean_span
    return frame_index(end) - frame_index(start)


def _portrait_slot_from_window(window: dict) -> dict:
    return {
        "slot_id": str(window.get("window_id") or ""),
        "start_frame": int(window.get("start_frame", 0) or 0),
        "end_frame": int(window.get("end_frame", 0) or 0),
        "unit_ids": list(window.get("unit_ids") or []),
        "boundary_source": window.get("boundary_source"),
    }


def _broll_slot_from_window(window: dict) -> dict:
    start_frame = int(window.get("start_frame", 0) or 0)
    end_frame = int(window.get("end_frame", 0) or 0)
    source_length_frames = int(
        window.get("source_length_frames") or max(0, end_frame - start_frame)
    )
    return {
        "slot_id": str(window.get("window_id") or ""),
        "start_frame": start_frame,
        "end_frame": end_frame,
        "length_frames": int(window.get("length_frames") or max(0, end_frame - start_frame)),
        "source_length_frames": source_length_frames,
        "pad_start": float(window.get("pad_start", 0) or 0),
        "pad_end": float(window.get("pad_end", 0) or 0),
        "unit_ids": list(window.get("host_unit_ids") or window.get("unit_ids") or []),
        "boundary_source": window.get("boundary_source"),
        "text": str(window.get("text") or ""),
    }


def _default_portrait_assignment(windows: dict) -> list[dict]:
    default_assignment = windows.get("default_assignment") or {}
    defaults = [
        item
        for item in (default_assignment.get("portrait") or [])
        if isinstance(item, dict)
    ]
    portrait_windows = [
        item for item in (windows.get("portrait_windows") or []) if isinstance(item, dict)
    ]
    assignment: list[dict] = []
    for window_data, default in zip(portrait_windows, defaults):
        segment_payload = default.get("segment_payload") or {}
        assignment.append(
            {
                "window_id": str(window_data.get("window_id") or ""),
                "candidate_id": str(default.get("window_id") or ""),
                "source_mode": str(segment_payload.get("source_mode") or "lipsynced"),
                "reason": "compiler default",
            }
        )
    return assignment


def _default_portrait_payload(windows: dict) -> dict:
    default_assignment = windows.get("default_assignment") or {}
    return dict(default_assignment.get("portrait_plan_payload") or {})


def _retrieval_topk_by_window(retrieval: dict | None) -> dict[str, list[str]]:
    candidates_by_window = retrieval.get("candidates_by_window") if isinstance(retrieval, dict) else {}
    if not isinstance(candidates_by_window, dict):
        return {}
    topk: dict[str, list[str]] = {}
    for window_id, candidates in candidates_by_window.items():
        if not isinstance(candidates, list):
            continue
        ids = [
            str(candidate.get("candidate_id") or "")
            for candidate in candidates
            if isinstance(candidate, dict) and str(candidate.get("candidate_id") or "")
        ]
        topk[str(window_id)] = ids
    return topk


def _selection_portrait_assignment(selection) -> list[dict]:
    return [
        {
            "window_id": choice.slot_id,
            "candidate_id": choice.window_id,
            "source_mode": choice.source_mode or "lipsynced",
            "reason": choice.reason,
        }
        for choice in selection.portrait
    ]


def _selection_broll_assignment(selection) -> list[dict]:
    return [
        {
            "window_id": choice.slot_id,
            "candidate_id": choice.candidate_id,
            "reason": choice.reason,
            "confidence": choice.confidence,
            "matched_keywords": list(choice.matched_keywords),
        }
        for choice in selection.broll
    ]


def _assignment_payload(
    *,
    engine: str,
    portrait: list[dict],
    broll: list[dict],
    font_id: str | None,
    bgm_id: str | None,
    diagnostics: dict,
) -> dict:
    return MediaAssignmentPlan(
        engine=engine,
        portrait=portrait,
        broll=broll,
        font_id=font_id,
        bgm_id=bgm_id,
        diagnostics=diagnostics,
    ).model_dump(mode="json")


def _record_llm_request_artifact(
    *,
    ctx: NodeContext,
    profile,
    prompt_invocation,
    rendered_prompt: str,
    attempt: int,
    previous_errors: list[str],
) -> Artifact:
    payload = {
        "capability_id": "llm.chat",
        "provider_profile_id": profile.id,
        "provider_id": getattr(profile, "provider_id", None),
        "model_id": getattr(profile, "model_id", None),
        "prompt_version_id": prompt_invocation.prompt_version_id,
        "prompt_invocation_id": prompt_invocation.id,
        "attempt": attempt,
        "repair_errors": list(previous_errors),
        "prompt": rendered_prompt,
    }
    return ctx.artifact(
        ArtifactKind.provider_raw_request,
        payload,
        "EditingAgentLlmRequestSnapshot.v1",
    )


def _limited_ids(values: list[str] | None, *, limit: int = _PROMPT_RETRIEVAL_TOPK_LIMIT) -> list[str]:
    ids: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
        if len(ids) >= limit:
            break
    return ids


def _compact_prompt_slot(slot: dict, *, broll: bool) -> dict:
    topk = _limited_ids(slot.get("retrieval_topk_candidate_ids") or [])
    raw_legal_ids = _limited_ids(slot.get("legal_window_ids") or [], limit=10_000)
    legal_ids = _limited_ids(raw_legal_ids)
    if topk and not broll:
        legal_ids = [candidate_id for candidate_id in topk if candidate_id in set(raw_legal_ids)]
        topk = legal_ids
    payload = {
        "slot_id": str(slot.get("slot_id") or ""),
        "required_seconds": slot.get("required_seconds"),
    }
    if broll:
        payload["text"] = str(slot.get("text") or "")
        payload["multi_clip_allowed"] = bool(slot.get("multi_clip_allowed"))
    else:
        payload["legal_window_ids"] = legal_ids
    if topk:
        payload["retrieval_topk_candidate_ids"] = topk
    return payload


def _prompt_line_cell(value) -> str:
    if isinstance(value, list | tuple):
        cells = [_prompt_line_cell(item) for item in value]
        return ",".join(cell for cell in cells if cell)
    text = "" if value is None else str(value)
    return " ".join(text.replace("|", "/").split())


def _prompt_candidate_lines(header: str, rows: list[list[object]]) -> str:
    lines = [header]
    lines.extend(" | ".join(_prompt_line_cell(value) for value in row) for row in rows)
    return "\n".join(lines)


def _compact_prompt_input(agent_input: dict) -> dict:
    """Shrink the LLM prompt to ID-decision fields only.

    The full candidate objects stay in ``EditingAgentContext.candidates`` for
    validation and materialization. This payload is only for the model prompt,
    so it omits frame/source bookkeeping that local code owns.
    """

    compact_broll_slots = [
        _compact_prompt_slot(slot, broll=True)
        for slot in agent_input.get("broll_slots", [])
        if isinstance(slot, dict)
    ]
    broll_allowed_slot_ids: dict[str, list[str]] = {}
    for slot in compact_broll_slots:
        slot_id = str(slot.get("slot_id") or "")
        for candidate_id in slot.get("retrieval_topk_candidate_ids") or []:
            text = str(candidate_id or "")
            if not text:
                continue
            broll_allowed_slot_ids.setdefault(text, []).append(slot_id)
    bgm_candidates = sorted(
        [item for item in agent_input.get("bgm_candidates", []) if isinstance(item, dict)],
        key=lambda item: float(item.get("score") or 0.0),
        reverse=True,
    )[:_PROMPT_BGM_CANDIDATE_LIMIT]
    portrait_rows = [
        [
            candidate.get("candidate_id"),
            candidate.get("asset_id"),
            candidate.get("available_seconds"),
            candidate.get("description"),
            candidate.get("reason"),
        ]
        for candidate in agent_input.get("portrait_candidates", [])
        if isinstance(candidate, dict)
    ]
    broll_rows = [
        [
            candidate.get("candidate_id"),
            candidate.get("asset_id"),
            candidate.get("scene_name"),
            broll_allowed_slot_ids.get(str(candidate.get("candidate_id") or ""), []),
            list(candidate.get("matched_keywords") or [])[:6],
            candidate.get("available_seconds"),
            candidate.get("description"),
        ]
        for candidate in agent_input.get("broll_candidates", [])
        if isinstance(candidate, dict)
        and broll_allowed_slot_ids.get(str(candidate.get("candidate_id") or ""))
    ]
    return {
        **agent_input,
        "narration_units": [
            {
                "unit_id": str(unit.get("unit_id") or ""),
                "text": str(unit.get("text") or ""),
                "start": unit.get("start"),
                "end": unit.get("end"),
            }
            for unit in agent_input.get("narration_units", [])
            if isinstance(unit, dict)
        ],
        "safe_cut_boundaries": [],
        "portrait_slots": [
            _compact_prompt_slot(slot, broll=False)
            for slot in agent_input.get("portrait_slots", [])
            if isinstance(slot, dict)
        ],
        "broll_slots": compact_broll_slots,
        "portrait_candidates": _prompt_candidate_lines(_PORTRAIT_CANDIDATE_HEADER, portrait_rows),
        "broll_candidates": _prompt_candidate_lines(_BROLL_CANDIDATE_HEADER, broll_rows),
        "bgm_candidates": [
            {
                "bgm_id": str(candidate.get("bgm_id") or ""),
                "mood": str(candidate.get("mood") or ""),
                "energy_profile": str(candidate.get("energy_profile") or ""),
                "script_fit": list(candidate.get("script_fit") or [])[:2],
                "scene_fit": list(candidate.get("scene_fit") or [])[:2],
            }
            for candidate in bgm_candidates
        ],
    }


def _local_str(value) -> str:
    return str(value).strip() if value is not None else ""


def _local_meta(candidate: dict) -> dict:
    meta = candidate.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _local_list(value) -> list[str]:
    if isinstance(value, list):
        return [_local_str(item) for item in value if _local_str(item)]
    if value:
        return [_local_str(value)]
    return []


def _broll_source_frames(candidate: dict) -> int:
    meta = _local_meta(candidate)
    start = float(meta.get("source_start", 0.0) or 0.0)
    end = float(meta.get("source_end", 0.0) or 0.0)
    return max(0, frame_index(end) - frame_index(start))


def _slot_source_required_frames(slot: dict) -> int:
    if slot.get("source_length_frames") is not None:
        return max(0, int(slot.get("source_length_frames", 0) or 0))
    return max(
        0,
        int(slot.get("end_frame", 0) or 0) - int(slot.get("start_frame", 0) or 0),
    )


def _broll_candidate_similarity(candidate: dict, desired: dict | None) -> tuple[int, int, int]:
    if desired is None:
        return (0, 0, 0)
    candidate_meta = _local_meta(candidate)
    desired_meta = _local_meta(desired)
    candidate_keywords = set(_local_list(candidate_meta.get("matched_keywords")))
    desired_keywords = set(_local_list(desired_meta.get("matched_keywords")))
    overlap = len(candidate_keywords & desired_keywords)
    same_scene = int(
        bool(candidate_meta.get("scene_name"))
        and candidate_meta.get("scene_name") == desired_meta.get("scene_name")
    )
    same_diversity = int(
        bool(candidate_meta.get("diversity_key"))
        and candidate_meta.get("diversity_key") == desired_meta.get("diversity_key")
    )
    return (overlap, same_scene, same_diversity)


def _repair_portrait_selection_to_constraints(
    *,
    selection: EditingSelection,
    boundary: dict,
    candidates: IndexedCandidates,
    bgm_enabled: bool,
    retrieval_topk_by_window: dict[str, list[str]],
    allow_broll_asset_diversity_reuse: bool = False,
) -> tuple[EditingSelection, list[dict], list[str]]:
    portrait_slots = {
        _local_str(slot.get("slot_id")): slot
        for slot in (boundary.get("portrait_slots") or [])
        if isinstance(slot, dict)
    }
    used_slots: set[str] = set()
    used_assets: set[str] = set()
    repaired_portrait: list[PortraitChoice] = []
    actions: list[dict] = []

    def asset_id(candidate_id: str) -> str:
        candidate = candidates.portrait_by_id.get(candidate_id)
        return _local_str(candidate.get("asset_id")) if candidate is not None else ""

    def usable(candidate_id: str, slot: dict) -> bool:
        candidate = candidates.portrait_by_id.get(candidate_id)
        if candidate is None:
            return False
        if (
            _agent_slot_has_retrieval_constraint(slot, retrieval_topk_by_window)
            and candidate_id not in set(_agent_topk_for_slot(slot, retrieval_topk_by_window))
        ):
            return False
        if _agent_source_frames_available(candidate) < _agent_slot_required_frames(slot):
            return False
        asset = asset_id(candidate_id)
        return not (asset and asset in used_assets)

    for choice in selection.portrait:
        slot = portrait_slots.get(choice.slot_id)
        choice_is_valid = (
            slot is not None
            and choice.slot_id not in used_slots
            and usable(choice.window_id, slot)
        )
        if choice_is_valid:
            used_slots.add(choice.slot_id)
            asset = asset_id(choice.window_id)
            if asset:
                used_assets.add(asset)
            repaired_portrait.append(choice)
            continue
        if slot is None or choice.slot_id in used_slots:
            repaired_portrait.append(choice)
            continue

        topk = _agent_topk_for_slot(slot, retrieval_topk_by_window)
        legal_pool = [candidate_id for candidate_id in topk if usable(candidate_id, slot)]
        original_asset = asset_id(choice.window_id)
        replacement_id = next(
            (
                candidate_id
                for candidate_id in legal_pool
                if original_asset and asset_id(candidate_id) == original_asset
            ),
            "",
        ) or next(iter(legal_pool), "")
        if not replacement_id:
            repaired_portrait.append(choice)
            continue

        used_slots.add(choice.slot_id)
        asset = asset_id(replacement_id)
        if asset:
            used_assets.add(asset)
        repaired_portrait.append(
            PortraitChoice(
                slot_id=choice.slot_id,
                window_id=replacement_id,
                source_mode=choice.source_mode,
                reason=choice.reason
                if replacement_id == choice.window_id
                else f"{choice.reason}（本地约束修正：{choice.window_id} -> {replacement_id}）",
            )
        )
        actions.append(
            {
                "slot_id": choice.slot_id,
                "original_window_id": choice.window_id,
                "repaired_window_id": replacement_id,
                "action": "replaced",
                "reason": "matched legal portrait retrieval candidate",
            }
        )

    repaired = EditingSelection(
        portrait=repaired_portrait,
        broll=selection.broll,
        font_id=selection.font_id,
        bgm_id=selection.bgm_id,
        analysis=selection.analysis,
        overreach_fields=selection.overreach_fields,
    )
    errors = validate_selection(
        repaired,
        boundary=boundary,
        candidates=candidates,
        bgm_enabled=bgm_enabled,
        retrieval_topk_by_window=retrieval_topk_by_window,
        allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
    )
    return repaired, actions, errors


def _repair_broll_selection_to_constraints(
    *,
    selection: EditingSelection,
    boundary: dict,
    candidates: IndexedCandidates,
    bgm_enabled: bool,
    max_inserts: int,
    retrieval_topk_by_window: dict[str, list[str]],
    allow_asset_diversity_reuse: bool = False,
    require_broll_coverage: bool = False,
) -> tuple[EditingSelection, list[dict], list[str]]:
    broll_slots = {
        _local_str(slot.get("slot_id")): slot
        for slot in (boundary.get("broll_slots") or [])
        if isinstance(slot, dict)
    }
    used_slots: set[str] = set()
    used_candidates: set[str] = set()
    used_assets: set[str] = set()
    used_diversity: set[str] = set()
    repaired_broll: list[BrollChoice] = []
    actions: list[dict] = []

    def usable(candidate_id: str, slot: dict) -> bool:
        candidate = candidates.broll_by_id.get(candidate_id)
        if candidate is None:
            return False
        topk = {
            _local_str(item)
            for item in retrieval_topk_by_window.get(_local_str(slot.get("slot_id")), [])
            if _local_str(item)
        }
        if topk and candidate_id not in topk:
            return False
        if candidate_id in used_candidates:
            return False
        asset_id = _local_str(candidate.get("asset_id"))
        if not allow_asset_diversity_reuse and asset_id and asset_id in used_assets:
            return False
        diversity_key = _local_str(_local_meta(candidate).get("diversity_key"))
        if (
            not allow_asset_diversity_reuse
            and diversity_key
            and diversity_key in used_diversity
        ):
            return False
        source_frames = _broll_source_frames(candidate)
        if allow_asset_diversity_reuse:
            return source_frames >= _slot_source_required_frames(slot)
        return source_frames >= _slot_source_required_frames(slot)

    def reserve(candidate_id: str) -> None:
        candidate = candidates.broll_by_id[candidate_id]
        used_candidates.add(candidate_id)
        asset_id = _local_str(candidate.get("asset_id"))
        if asset_id:
            used_assets.add(asset_id)
        diversity_key = _local_str(_local_meta(candidate).get("diversity_key"))
        if diversity_key:
            used_diversity.add(diversity_key)

    def replacement_pool(slot_id: str, desired: dict | None, preferred_id: str = "") -> list[str]:
        pool = [
            candidate_id
            for candidate_id in retrieval_topk_by_window.get(slot_id, [])
            if candidate_id in candidates.broll_by_id
        ] or list(candidates.broll_by_id)
        ranked_pool = sorted(
            enumerate(pool),
            key=lambda item: (
                -int(bool(preferred_id) and item[1] == preferred_id),
                -_broll_candidate_similarity(candidates.broll_by_id[item[1]], desired)[0],
                -_broll_candidate_similarity(candidates.broll_by_id[item[1]], desired)[1],
                -_broll_candidate_similarity(candidates.broll_by_id[item[1]], desired)[2],
                item[0],
            ),
        )
        return [candidate_id for _, candidate_id in ranked_pool]

    for choice in selection.broll:
        if len(repaired_broll) >= max(0, max_inserts):
            actions.append(
                {
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "action": "dropped",
                    "reason": "max_broll_inserts reached",
                }
            )
            continue
        slot = broll_slots.get(choice.slot_id)
        if slot is None or choice.slot_id in used_slots:
            actions.append(
                {
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "action": "dropped",
                    "reason": "unknown or duplicate slot",
                }
            )
            continue
        desired = candidates.broll_by_id.get(choice.candidate_id)
        candidate_id = next(
            (
                candidate_id
                for candidate_id in replacement_pool(choice.slot_id, desired, choice.candidate_id)
                if usable(candidate_id, slot)
            ),
            "",
        )
        if not candidate_id:
            actions.append(
                {
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "action": "dropped",
                    "reason": "no legal broll candidate remains",
                }
            )
            used_slots.add(choice.slot_id)
            continue
        used_slots.add(choice.slot_id)
        reserve(candidate_id)
        repaired_broll.append(
            BrollChoice(
                slot_id=choice.slot_id,
                candidate_id=candidate_id,
                reason=choice.reason
                if candidate_id == choice.candidate_id
                else f"{choice.reason}（本地约束修正：{choice.candidate_id} -> {candidate_id}）",
                confidence=choice.confidence,
                matched_keywords=choice.matched_keywords,
            )
        )
        if candidate_id != choice.candidate_id:
            actions.append(
                {
                    "slot_id": choice.slot_id,
                    "original_candidate_id": choice.candidate_id,
                    "repaired_candidate_id": candidate_id,
                    "action": "replaced",
                    "reason": "matched nearest legal retrieval/diversity candidate",
                }
            )

    if require_broll_coverage:
        covered_by_slot: dict[str, int] = {}
        for choice in repaired_broll:
            candidate = candidates.broll_by_id.get(choice.candidate_id)
            if candidate is None:
                continue
            covered_by_slot[choice.slot_id] = covered_by_slot.get(
                choice.slot_id,
                0,
            ) + _broll_source_frames(candidate)
        for slot_id, slot in broll_slots.items():
            required_frames = _slot_source_required_frames(slot)
            covered_frames = covered_by_slot.get(slot_id, 0)
            pool = [
                candidate_id
                for candidate_id in retrieval_topk_by_window.get(slot_id, [])
                if candidate_id in candidates.broll_by_id
            ] or list(candidates.broll_by_id)
            while covered_frames < required_frames and len(repaired_broll) < max(0, max_inserts):
                was_missing_slot = covered_frames == 0
                candidate_id = next(
                    (candidate_id for candidate_id in pool if usable(candidate_id, slot)),
                    "",
                )
                if not candidate_id:
                    break
                candidate = candidates.broll_by_id[candidate_id]
                reserve(candidate_id)
                source_frames = _broll_source_frames(candidate)
                meta = _local_meta(candidate)
                repaired_broll.append(
                    BrollChoice(
                        slot_id=slot_id,
                        candidate_id=candidate_id,
                        reason="本地 full_coverage 补窗",
                        confidence=0.5,
                        matched_keywords=tuple(_local_list(meta.get("matched_keywords"))),
                    )
                )
                covered_frames += source_frames
                actions.append(
                    {
                        "slot_id": slot_id,
                        "repaired_candidate_id": candidate_id,
                        "action": "filled" if was_missing_slot else "added",
                        "reason": (
                            "filled missing full_coverage broll slot from legal retrieval candidate"
                            if was_missing_slot
                            else "filled full_coverage window gap"
                        ),
                        "covered_frames": min(covered_frames, required_frames),
                        "required_frames": required_frames,
                    }
                )

    repaired = EditingSelection(
        portrait=selection.portrait,
        broll=repaired_broll,
        font_id=selection.font_id,
        bgm_id=selection.bgm_id,
        analysis=selection.analysis,
        overreach_fields=selection.overreach_fields,
    )
    errors = validate_selection(
        repaired,
        boundary=boundary,
        candidates=candidates,
        bgm_enabled=bgm_enabled,
        retrieval_topk_by_window=retrieval_topk_by_window,
        allow_broll_asset_diversity_reuse=allow_asset_diversity_reuse,
        require_broll_coverage=require_broll_coverage,
    )
    return repaired, actions, errors


def _record_llm_response_artifact(
    *,
    ctx: NodeContext,
    invocation,
    result,
    attempt: int,
) -> Artifact:
    payload = {
        "capability_id": "llm.chat",
        "provider_invocation_id": invocation.id,
        "provider_profile_id": getattr(invocation, "provider_profile_id", None),
        "provider_id": getattr(invocation, "provider_id", None),
        "model_id": getattr(invocation, "model_id", None),
        "prompt_version_id": getattr(invocation, "prompt_version_id", None),
        "attempt": attempt,
        "status": _enum_value(getattr(invocation, "status", "unknown")),
        "error": getattr(invocation, "error", None).model_dump(mode="json")
        if getattr(invocation, "error", None)
        else None,
        "output": result.output if result is not None else None,
    }
    return ctx.artifact(
        ArtifactKind.provider_raw_response,
        payload,
        "EditingAgentLlmResponseSnapshot.v1",
    )


def _attach_provider_artifacts(
    *, ctx: NodeContext, invocation_id: str, request_artifact: Artifact, response_artifact: Artifact
) -> None:
    current = ctx.repository.provider_invocations.get(invocation_id)
    if current is None:
        return
    ctx.repository.provider_invocations[invocation_id] = current.model_copy(
        update={
            "request_artifact_id": request_artifact.id,
            "response_artifact_id": response_artifact.id,
            "updated_at": utcnow(),
        }
    )


def build_editing_agent_context(
    *,
    request,
    material: dict,
    narration: dict,
    boundary: dict,
    windows: dict,
    creative_intent,
    retrieval: dict | None = None,
) -> EditingAgentContext:
    raw_units = narration.get("units", []) or []
    duration = max([float(unit.get("end", 0) or 0) for unit in raw_units] or [1.0])
    huazi_candidates = [
        event.model_dump(mode="json")
        for event in derive_huazi_candidates(creative_intent.emphasis, raw_units)
    ]

    agent_boundary = _boundary_with_compiled_windows(boundary, windows)
    shortlisted_material, shortlist_counts = shortlist_for_windows(
        windows.get("portrait_windows", []) or [],
        windows.get("broll_windows", []) or [],
        material,
    )
    retrieval_topk_by_window = _retrieval_topk_by_window(retrieval)
    if retrieval is not None:
        for slot in [
            *(agent_boundary.get("portrait_slots") or []),
            *(agent_boundary.get("broll_slots") or []),
        ]:
            if isinstance(slot, dict):
                slot_id = str(slot.get("slot_id") or "")
                if slot_id:
                    retrieval_topk_by_window.setdefault(slot_id, [])
    candidate_material = material if retrieval is not None else shortlisted_material
    candidates = index_candidates(candidate_material)
    prompt_candidates = (
        _prompt_candidates_for_retrieval(candidates, retrieval_topk_by_window)
        if retrieval is not None
        else candidates
    )
    agent_input = build_agent_input(
        request=request,
        boundary=agent_boundary,
        candidates=prompt_candidates,
        narration_units=raw_units,
        duration=duration,
        retrieval_topk_by_window=retrieval_topk_by_window,
    )
    portrait_feasibility_failure = _portrait_feasibility_failure(agent_input)
    if portrait_feasibility_failure is not None:
        portrait_feasibility_failure.update(_raw_portrait_candidate_diagnostics(material))
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "人像素材不足：portrait slot 没有可覆盖的源窗口。",
            details=portrait_feasibility_failure,
        )

    return EditingAgentContext(
        material=material,
        narration=narration,
        boundary=boundary,
        windows=windows,
        raw_units=raw_units,
        duration=duration,
        agent_boundary=agent_boundary,
        shortlisted_material=shortlisted_material,
        shortlist_counts=shortlist_counts,
        candidates=candidates,
        retrieval_topk_by_window=retrieval_topk_by_window,
        huazi_candidates=huazi_candidates,
        agent_input=agent_input,
    )


def _prompt_candidates_for_retrieval(
    candidates: IndexedCandidates,
    retrieval_topk_by_window: dict[str, list[str]],
) -> IndexedCandidates:
    allowed_ids = {
        candidate_id
        for candidate_ids in retrieval_topk_by_window.values()
        for candidate_id in candidate_ids
    }
    return IndexedCandidates(
        portrait_by_id={
            candidate_id: candidate
            for candidate_id, candidate in candidates.portrait_by_id.items()
            if candidate_id in allowed_ids
        },
        broll_by_id={
            candidate_id: candidate
            for candidate_id, candidate in candidates.broll_by_id.items()
            if candidate_id in allowed_ids
        },
        font_by_id=candidates.font_by_id,
        bgm_by_id=candidates.bgm_by_id,
    )


def select_editing_assignment(
    *,
    ctx: NodeContext,
    agent_context: EditingAgentContext,
) -> EditingAgentSelectionResult:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    degradations: list[DegradationNotice] = []
    warnings: list[WarningCode] = []
    provider_invocation_ids: list[str] = []
    repair_trace: list[dict] = []
    fallback_used = False
    fallback_reason: str | None = None
    allow_broll_asset_diversity_reuse = broll_full_coverage_enabled(state.request)
    require_broll_coverage = broll_full_coverage_enabled(state.request)

    def _validate_deterministic_fallback(selection: EditingSelection) -> None:
        if not agent_context.retrieval_topk_by_window:
            return
        errors = validate_selection(
            selection,
            boundary=agent_context.agent_boundary,
            candidates=agent_context.candidates,
            bgm_enabled=state.request.bgm.enabled,
            retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
            require_broll_coverage=require_broll_coverage,
        )
        if not errors:
            return
        error_code = (
            ErrorCode.material_insufficient_portrait
            if any(error.startswith("portrait slots not covered:") for error in errors)
            else ErrorCode.material_insufficient_broll
            if any(error.startswith("broll slots not covered:") for error in errors)
            else ErrorCode.prompt_output_invalid
        )
        raise NodeExecutionError(
            error_code,
            "确定性兜底选择无法满足窗口检索约束：" + "；".join(errors[:5]),
            details={"errors": errors},
        )

    if profile is None:
        if not sandbox_fallback_allowed():
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "未配置可用的真实 LLM 供应商（llm.chat）。"
                "请在「设置」中配置并启用真实 LLM 供应商及密钥。",
            )
        broll_limit = _broll_assignment_limit(
            request=state.request,
            windows=agent_context.windows,
            broll_candidate_count=len(agent_context.candidates.broll_by_id),
        )
        selection = deterministic_selection(
            boundary=agent_context.agent_boundary,
            candidates=agent_context.candidates,
            bgm_enabled=state.request.bgm.enabled,
            max_inserts=broll_limit,
            retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
        )
        _validate_deterministic_fallback(selection)
        engine = "deterministic_fallback"
        fallback_used = True
        fallback_reason = "no_provider"
        degradations.append(
            degradation_notice(
                WarningCode.editing_agent_deterministic_fallback,
                "剪辑 Agent 无可用真实 LLM 供应商，改用确定性兜底选择。",
                node_id=node_run.node_id,
                affects_true_yield=False,
            )
        )
        warnings.append(WarningCode.editing_agent_deterministic_fallback)
    else:
        engine = "editing_agent_llm"
        prompt_input = _compact_prompt_input(agent_context.agent_input)

        def _invoke(previous_errors: list[str]):
            attempt = len(provider_invocation_ids)
            prompt_invocation, rendered = ctx.prompt_registry.render(
                node_id="EditingAgentPlanning",
                variables=_prompt_variables(prompt_input, previous_errors),
                case_id=run.case_id,
                run_id=run.id,
                node_run_id=node_run.id,
                provider_profile_id=profile.id,
            )
            request_artifact = _record_llm_request_artifact(
                ctx=ctx,
                profile=profile,
                prompt_invocation=prompt_invocation,
                rendered_prompt=rendered,
                attempt=attempt,
                previous_errors=previous_errors,
            )
            invocation, result = ctx.provider_gateway.invoke(
                ProviderCall(
                    case_id=run.case_id,
                    run_id=run.id,
                    node_run_id=node_run.id,
                    provider_profile_id=profile.id,
                    capability_id="llm.chat",
                    prompt_version_id=prompt_invocation.prompt_version_id,
                    input={
                        "prompt": rendered,
                        "response_format": {"type": "json_object"},
                    },
                    idempotency_key=f"{run.id}:{node_run.id}:editing_agent:{attempt}",
                )
            )
            response_artifact = _record_llm_response_artifact(
                ctx=ctx,
                invocation=invocation,
                result=result,
                attempt=attempt,
            )
            _attach_provider_artifacts(
                ctx=ctx,
                invocation_id=invocation.id,
                request_artifact=request_artifact,
                response_artifact=response_artifact,
            )
            if result is None or invocation.error:
                raise NodeExecutionError(
                    invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                    invocation.error.message
                    if invocation.error
                    else "Editing agent provider failed.",
                    retryable=True,
                )
            provider_invocation_ids.append(invocation.id)
            ctx.prompt_registry.validate_output(
                prompt_version_id=prompt_invocation.prompt_version_id, output=result.output
            )
            # llm.chat providers (e.g. DashScope) wrap the model's parsed JSON under
            # ``output["intent"]`` (mirrors resolve_creative_intent.py) — the ID selection
            # lives there, NOT at the top level. Unwrap before parse_selection, falling back
            # to the raw dict for a provider that already returns the selection flat.
            payload = result.output if isinstance(result.output, dict) else {}
            nested = payload.get("intent")
            return nested if isinstance(nested, dict) else payload

        selection, repair_trace, errors = select_with_repair(
            invoke=_invoke,
            boundary=agent_context.agent_boundary,
            candidates=agent_context.candidates,
            bgm_enabled=state.request.bgm.enabled,
            max_repair_attempts=state.request.edit.max_repair_attempts,
            retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
            require_broll_coverage=require_broll_coverage,
        )
        llm_repair_used = any(
            isinstance(item.get("attempt"), int) and int(item.get("error_count") or 0) > 0
            for item in repair_trace
            if isinstance(item, dict)
        )
        if errors:
            repaired_selection, portrait_repair_actions, portrait_repair_errors = (
                _repair_portrait_selection_to_constraints(
                    selection=selection,
                    boundary=agent_context.agent_boundary,
                    candidates=agent_context.candidates,
                    bgm_enabled=state.request.bgm.enabled,
                    retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
                    allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
                )
            )
            if portrait_repair_actions:
                repair_trace.append(
                    {
                        "attempt": "local_constraint_repair_portrait",
                        "error_count": len(portrait_repair_errors),
                        "errors": portrait_repair_errors,
                        "actions": portrait_repair_actions,
                    }
                )
            repaired_selection, local_repair_actions, local_repair_errors = (
                _repair_broll_selection_to_constraints(
                    selection=repaired_selection,
                    boundary=agent_context.agent_boundary,
                    candidates=agent_context.candidates,
                    bgm_enabled=state.request.bgm.enabled,
                    max_inserts=_broll_assignment_limit(
                        request=state.request,
                        windows=agent_context.windows,
                        broll_candidate_count=len(agent_context.candidates.broll_by_id),
                    ),
                    retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
                    allow_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
                    require_broll_coverage=require_broll_coverage,
                )
            )
            if local_repair_actions:
                repair_trace.append(
                    {
                        "attempt": "local_constraint_repair",
                        "error_count": len(local_repair_errors),
                        "errors": local_repair_errors,
                        "actions": local_repair_actions,
                    }
                )
            errors = local_repair_errors
            if not local_repair_errors:
                selection = repaired_selection
                errors = []
                if WarningCode.editing_agent_local_constraint_repair not in warnings:
                    warnings.append(WarningCode.editing_agent_local_constraint_repair)
        if errors:
            if not sandbox_fallback_allowed():
                raise NodeExecutionError(
                    ErrorCode.prompt_output_invalid,
                    f"剪辑 Agent 的选择在 {state.request.edit.max_repair_attempts} "
                    "次修复后仍不合法："
                    + "；".join(errors[:5]),
                )
            selection = deterministic_selection(
                boundary=agent_context.agent_boundary,
                candidates=agent_context.candidates,
                bgm_enabled=state.request.bgm.enabled,
                max_inserts=_broll_assignment_limit(
                    request=state.request,
                    windows=agent_context.windows,
                    broll_candidate_count=len(agent_context.candidates.broll_by_id),
                ),
                retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
                allow_broll_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
            )
            _validate_deterministic_fallback(selection)
            engine = "deterministic_fallback"
            fallback_used = True
            fallback_reason = "llm_unrepairable"
            degradations.append(
                degradation_notice(
                    WarningCode.editing_agent_deterministic_fallback,
                    "剪辑 Agent 输出不可修复，改用确定性兜底选择。",
                    node_id=node_run.node_id,
                    affects_true_yield=False,
                )
            )
            warnings.append(WarningCode.editing_agent_deterministic_fallback)
        elif llm_repair_used:
            warnings.append(WarningCode.editing_agent_llm_repair)

    return EditingAgentSelectionResult(
        selection=selection,
        engine=engine,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        repair_trace=repair_trace,
        warnings=warnings,
        degradations=degradations,
        provider_invocation_ids=provider_invocation_ids,
    )


def materialize_editing_outputs(
    *,
    request,
    node_id: str,
    agent_context: EditingAgentContext,
    selection_result: EditingAgentSelectionResult,
    huazi_result: HuaziPlanningResult,
) -> EditingAgentMaterializedOutputs:
    selection = selection_result.selection
    warnings = list(selection_result.warnings)
    degradations = list(selection_result.degradations)
    windows = agent_context.windows
    candidates = agent_context.candidates
    fallback_used = selection_result.fallback_used
    use_default_portrait = fallback_used and not agent_context.retrieval_topk_by_window

    portrait_assignment = (
        _default_portrait_assignment(windows)
        if use_default_portrait
        else _selection_portrait_assignment(selection)
    )
    broll_assignment = _selection_broll_assignment(selection)
    broll_limit = _broll_assignment_limit(
        request=request,
        windows=windows,
        broll_candidate_count=len(candidates.broll_by_id),
    )
    assignment_for_materialize = {
        "portrait": portrait_assignment,
        "broll": broll_assignment,
    }
    portrait_payload = (
        _default_portrait_payload(windows)
        if use_default_portrait
        else materialize_portrait_from_assignment(
            windows=windows,
            assignment=assignment_for_materialize,
            candidates=candidates,
        )
    )
    if broll_full_coverage_enabled(request):
        broll_payload, broll_drops = materialize_full_coverage_broll_from_assignment(
            windows=windows,
            assignment=assignment_for_materialize,
            candidates=candidates,
            enabled=request.broll.enabled,
            max_inserts=broll_limit,
        )
    else:
        broll_payload, broll_drops = materialize_broll_from_assignment(
            windows=windows,
            assignment=assignment_for_materialize,
            candidates=candidates,
            cut_frames=portrait_cut_frames(portrait_payload),
            enabled=request.broll.enabled,
            max_inserts=broll_limit,
        )
    if broll_full_coverage_enabled(request):
        _ensure_full_coverage_broll(
            windows=windows,
            broll_payload=broll_payload,
            broll_drops=broll_drops,
        )
    if broll_drops:
        selected_broll_choices = selection.broll[: max(0, broll_limit)]
        affects_true_yield = bool(selected_broll_choices) and not broll_payload.get("overlays")
        degradations.append(
            degradation_notice(
                WarningCode.broll_insertions_dropped_geometry,
                f"B-roll 有 {len(broll_drops)} 个插入因时间线几何约束被丢弃。",
                node_id=node_id,
                affects_true_yield=affects_true_yield,
            ).model_copy(update={"details": {"broll_drops": broll_drops}})
        )
        warnings.append(WarningCode.broll_insertions_dropped_geometry)
    warnings.extend(huazi_result.warnings)
    degradations.extend(
        notice.model_copy(update={"node_id": node_id}) for notice in huazi_result.degradations
    )
    style_payload, style_warnings, style_degradations = materialize_style_from_selection(
        request=request,
        material=agent_context.shortlisted_material,
        overlay_events=huazi_result.overlay_events,
        bgm_id=selection.bgm_id,
    )
    warnings.extend(style_warnings)
    degradations.extend(
        notice.model_copy(update={"node_id": node_id}) for notice in style_degradations
    )

    assignment_diagnostics = {
        "repair_trace": selection_result.repair_trace,
        "shortlist_counts": agent_context.shortlist_counts,
        "retrieval_topk_by_window": agent_context.retrieval_topk_by_window,
        "fallback_used": selection_result.fallback_used,
        "fallback_reason": selection_result.fallback_reason,
        "broll_drops": broll_drops,
    }
    assignment_payload = _assignment_payload(
        engine=selection_result.engine,
        portrait=portrait_assignment,
        broll=broll_assignment,
        font_id=None,
        bgm_id=selection.bgm_id,
        diagnostics=assignment_diagnostics,
    )

    diagnostics = {
        "mode": selection_result.engine,
        "instruction": request.edit.instruction,
        "analysis": selection.analysis,
        "repair_trace": selection_result.repair_trace,
        "portrait_choices": [
            {
                "slot_id": item["window_id"],
                "window_id": item["candidate_id"],
                "reason": item["reason"],
            }
            for item in portrait_assignment
        ],
        "broll_choices": [
            {
                "slot_id": item["window_id"],
                "candidate_id": item["candidate_id"],
                "reason": item["reason"],
            }
            for item in broll_assignment
        ],
        "broll_drops": broll_drops,
        "font_id": None,
        "request_font_id": request.subtitle.font_id,
        "huazi_choices": huazi_result.diagnostics.get("choices", []),
        "huazi_diagnostics": {
            key: value for key, value in huazi_result.diagnostics.items() if key != "choices"
        },
        "bgm_id": selection.bgm_id,
        "shortlist_counts": agent_context.shortlist_counts,
        "retrieval_topk_by_window": agent_context.retrieval_topk_by_window,
        "fallback_used": selection_result.fallback_used,
        "fallback_reason": selection_result.fallback_reason,
        "candidate_counts": {
            "portrait": len(candidates.portrait_by_id),
            "broll": len(candidates.broll_by_id),
            "font": len(candidates.font_by_id),
            "bgm": len(candidates.bgm_by_id),
        },
    }

    return EditingAgentMaterializedOutputs(
        assignment_payload=assignment_payload,
        portrait_payload=portrait_payload,
        broll_payload=broll_payload,
        style_payload=style_payload,
        diagnostics=diagnostics,
        warnings=warnings,
        degradations=degradations,
    )


def _broll_assignment_limit(*, request, windows: dict, broll_candidate_count: int = 0) -> int:
    if broll_full_coverage_enabled(request):
        window_count = len([w for w in (windows.get("broll_windows") or []) if isinstance(w, dict)])
        return window_count
    return request.broll.max_inserts


def _ensure_full_coverage_broll(
    *,
    windows: dict,
    broll_payload: dict,
    broll_drops: list[dict],
) -> None:
    expected = {
        str(window.get("window_id") or "")
        for window in (windows.get("broll_windows") or [])
        if isinstance(window, dict) and str(window.get("window_id") or "")
    }
    covered = {
        str(overlay.get("window_id") or "")
        for overlay in (broll_payload.get("overlays") or [])
        if isinstance(overlay, dict) and str(overlay.get("window_id") or "")
    }
    missing = sorted(expected - covered)
    coverage_gaps = full_coverage_broll_coverage_gaps(
        windows=windows,
        overlays=[overlay for overlay in (broll_payload.get("overlays") or []) if isinstance(overlay, dict)],
    )
    if missing or coverage_gaps or broll_drops:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_broll,
            "B-roll full coverage requires every authoritative window to have material.",
            details={
                "missing_broll_window_ids": missing,
                "expected_broll_window_count": len(expected),
                "covered_broll_window_count": len(covered),
                "coverage_gaps": coverage_gaps,
                "broll_drops": broll_drops,
            },
        )


def _empty_huazi_result(reason: str) -> HuaziPlanningResult:
    return HuaziPlanningResult(
        overlay_events=[],
        warnings=[],
        degradations=[],
        provider_invocation_ids=[],
        diagnostics={"planned": False, "reason": reason, "choices": []},
    )


def _degraded_huazi_result(
    node_id: str,
    *,
    reason: str,
    repair_trace: list[dict],
    provider_invocation_ids: list[str],
    errors: list[str] | None = None,
    detail: str | None = None,
) -> HuaziPlanningResult:
    details: dict = {"reason": reason, "repair_trace": repair_trace}
    if errors:
        details["errors"] = errors[:5]
    if detail:
        details["provider_error"] = detail
    notice = degradation_notice(
        WarningCode.huazi_planning_failed,
        "花字编排未能生成有效结果，本条视频不加花字。",
        node_id=node_id,
        affects_true_yield=False,
    ).model_copy(update={"details": details})
    return HuaziPlanningResult(
        overlay_events=[],
        warnings=[WarningCode.huazi_planning_failed],
        degradations=[notice],
        provider_invocation_ids=provider_invocation_ids,
        diagnostics={
            "planned": False,
            "reason": reason,
            "degraded": True,
            "choices": [],
            "repair_trace": repair_trace,
        },
    )


def _huazi_prompt_variables(agent_input: dict, previous_errors: list[str]) -> dict:
    variables = {
        key: (json.dumps(value, ensure_ascii=False) if key in _HUAZI_JSON_VARS else str(value))
        for key, value in agent_input.items()
    }
    variables["repair_feedback"] = (
        "上一轮花字编排存在以下问题，请只修正这些点后重新只输出 JSON：\n- "
        + "\n- ".join(previous_errors)
        if previous_errors
        else ""
    )
    return variables


def _unit_text_for_event(event: dict, units: list[dict]) -> str:
    start = float(event.get("start", 0) or 0)
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_start = float(unit.get("start", 0) or 0)
        unit_end = float(unit.get("end", 0) or 0)
        if abs(unit_start - start) < 1e-6 or unit_start <= start < unit_end:
            return str(unit.get("text") or "")
    return ""


def _compact_huazi_box(box: dict) -> dict:
    return {
        "layout_box_id": box.get("layout_box_id"),
        "rect": box.get("rect"),
        "text_align": box.get("text_align"),
        "allowed_enter_directions": list(box.get("allowed_enter_directions") or []),
        "collision_score": box.get("collision_score"),
        "region_tags": list(box.get("region_tags") or []),
    }


def _huazi_track_summary(windows: dict) -> list[dict]:
    fps = int(windows.get("fps") or 30) or 30
    summary: list[dict] = []
    for window in windows.get("portrait_windows") or []:
        if not isinstance(window, dict):
            continue
        summary.append(
            {
                "track": "portrait",
                "start": round(int(window.get("start_frame", 0) or 0) / fps, 2),
                "end": round(int(window.get("end_frame", 0) or 0) / fps, 2),
            }
        )
    # B-roll windows are the final placement facts from TimelineWindowPlanning;
    # broll selection choices carry slot_ids (bslot_*), not window_ids (bwin_*),
    # so there is no per-choice filter here — include every placement window.
    for window in windows.get("broll_windows") or []:
        if not isinstance(window, dict):
            continue
        summary.append(
            {
                "track": "broll",
                "start": round(int(window.get("start_frame", 0) or 0) / fps, 2),
                "end": round(int(window.get("end_frame", 0) or 0) / fps, 2),
                "text": str(window.get("text") or ""),
            }
        )
    return sorted(summary, key=lambda item: (item["start"], item["track"]))


def plan_huazi_overlays(
    *,
    ctx: NodeContext,
    agent_context: EditingAgentContext,
    selection_result: EditingAgentSelectionResult,
) -> HuaziPlanningResult:
    """Second LLM pass: place emphasis captions (huazi) on the chosen timeline.

    Skips silently (no huazi, no degradation) when emphasis captions are disabled,
    there are no candidate phrases, or no real ``llm.chat`` provider is armed (the
    deterministic chain plans no huazi at all, D17). Otherwise it renders the
    HuaziPlanningSubagent prompt, validates the ID-only selection locally, repairs
    once, and — on unrepairable output or a provider error — degrades to no huazi
    with ``huazi.planning_failed`` (never a fabricated deterministic huazi, D16).
    """
    state = ctx.state
    request = state.request
    run = ctx.run
    node_run = ctx.node_run

    emphasis_enabled = bool(request.subtitle.enabled and request.subtitle.emphasis_enabled)
    candidates = agent_context.huazi_candidates
    if not emphasis_enabled:
        return _empty_huazi_result("emphasis_disabled")
    if not candidates:
        return _empty_huazi_result("no_candidates")
    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    if profile is None:
        return _empty_huazi_result("no_provider")

    resolution = (int(request.output.width), int(request.output.height))
    font_size = _subtitle_font_size(request.subtitle.style_preset, request.subtitle.font_size)
    position = _subtitle_position(request.subtitle.style_preset, request.subtitle.position)
    position_y = float(position.get("y", 0.84)) if isinstance(position, dict) else 0.84
    top_y = normal_caption_top_y(
        position_y=position_y, font_size=font_size, canvas_height=resolution[1]
    )

    candidate_events: list[dict] = []
    boxes_by_event: dict[str, list[dict]] = {}
    for event in candidates:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            continue
        text = str(event.get("text") or "")
        boxes_by_event[event_id] = generate_layout_boxes(
            event_text=text,
            resolution=resolution,
            normal_caption_top_y=top_y,
            neighbor_boxes=[],
        )
        candidate_events.append(
            {
                "event_id": event_id,
                "text": text,
                "start": round(float(event.get("start", 0) or 0), 3),
                "end": round(float(event.get("end", 0) or 0), 3),
                "unit_text": _unit_text_for_event(event, agent_context.raw_units),
            }
        )
    if not candidate_events:
        return _empty_huazi_result("no_candidates")

    agent_input = {
        "script": request.script,
        "track_summary": _huazi_track_summary(agent_context.windows),
        "normal_caption_zone": (
            f"普通字幕大致落在画面下方（归一化 y≈{position_y}）；只有 y<{top_y} 的上方区域才是"
            "花字安全区，候选框已按此过滤，不要担心遮挡普通字幕，专注避开人脸主体即可。"
        ),
        "candidate_events": candidate_events,
        "layout_boxes": {
            event_id: [_compact_huazi_box(box) for box in boxes]
            for event_id, boxes in boxes_by_event.items()
        },
        "animation_candidates": list(HUAZI_ANIMATIONS),
        "animation_directions": HUAZI_ANIMATION_DIRECTIONS,
    }

    provider_invocation_ids: list[str] = []

    def _invoke(previous_errors: list[str]):
        attempt = len(provider_invocation_ids)
        prompt_invocation, rendered = ctx.prompt_registry.render(
            node_id="HuaziPlanningSubagent",
            variables=_huazi_prompt_variables(agent_input, previous_errors),
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=profile.id,
        )
        request_artifact = _record_llm_request_artifact(
            ctx=ctx,
            profile=profile,
            prompt_invocation=prompt_invocation,
            rendered_prompt=rendered,
            attempt=attempt,
            previous_errors=previous_errors,
        )
        invocation, result = ctx.provider_gateway.invoke(
            ProviderCall(
                case_id=run.case_id,
                run_id=run.id,
                node_run_id=node_run.id,
                provider_profile_id=profile.id,
                capability_id="llm.chat",
                prompt_version_id=prompt_invocation.prompt_version_id,
                input={
                    "prompt": rendered,
                    "response_format": {"type": "json_object"},
                },
                idempotency_key=f"{run.id}:{node_run.id}:huazi_agent:{attempt}",
            )
        )
        response_artifact = _record_llm_response_artifact(
            ctx=ctx,
            invocation=invocation,
            result=result,
            attempt=attempt,
        )
        _attach_provider_artifacts(
            ctx=ctx,
            invocation_id=invocation.id,
            request_artifact=request_artifact,
            response_artifact=response_artifact,
        )
        if result is None or invocation.error:
            raise NodeExecutionError(
                invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                invocation.error.message if invocation.error else "Huazi subagent provider failed.",
                retryable=True,
            )
        provider_invocation_ids.append(invocation.id)
        # Same unwrap contract as the main editing agent: DashScope-style providers
        # nest the parsed JSON under ``output["intent"]``; fall back to the raw dict
        # for a provider that already returns the selection flat.
        payload = result.output if isinstance(result.output, dict) else {}
        nested = payload.get("intent")
        return nested if isinstance(nested, dict) else payload

    repair_trace: list[dict] = []
    errors: list[str] = []
    choices: list[HuaziPlanChoice] = []
    try:
        for attempt in range(_HUAZI_MAX_REPAIR_ATTEMPTS + 1):
            output = _invoke(errors)
            choices, overreach = parse_huazi_plan(output)
            errors = validate_huazi_plan(
                choices,
                candidate_events=candidate_events,
                boxes_by_event=boxes_by_event,
                overreach_fields=overreach,
            )
            repair_trace.append(
                {"attempt": attempt, "error_count": len(errors), "errors": errors}
            )
            if not errors:
                break
    except NodeExecutionError as exc:
        return _degraded_huazi_result(
            node_run.node_id,
            reason="provider_error",
            repair_trace=repair_trace,
            provider_invocation_ids=provider_invocation_ids,
            detail=str(exc.error.message if exc.error else exc),
        )

    if errors:
        return _degraded_huazi_result(
            node_run.node_id,
            reason="unrepairable",
            repair_trace=repair_trace,
            provider_invocation_ids=provider_invocation_ids,
            errors=errors,
        )

    finalized = finalize_huazi_plan(
        choices,
        candidate_events=candidate_events,
        boxes_by_event=boxes_by_event,
    )
    diagnostics = {
        "planned": True,
        "choices": finalized.choices,
        "animation_fallbacks": finalized.animation_fallbacks,
        "density_drops": finalized.density_drops,
        "repair_attempts": max(0, len(repair_trace) - 1),
        "candidate_count": len(candidate_events),
        "repair_trace": repair_trace,
    }
    return HuaziPlanningResult(
        overlay_events=finalized.overlay_events,
        warnings=[],
        degradations=[],
        provider_invocation_ids=provider_invocation_ids,
        diagnostics=diagnostics,
    )


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    boundary = state.require(ArtifactKind.plan_narration_boundary).payload or {}
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}
    retrieval_artifact = state.artifacts.get(ArtifactKind.plan_window_material_retrieval)
    retrieval = retrieval_artifact.payload if retrieval_artifact is not None else None
    creative_intent = load_creative_intent(state)

    agent_context = build_editing_agent_context(
        request=state.request,
        material=material,
        narration=narration,
        boundary=boundary,
        windows=windows,
        creative_intent=creative_intent,
        retrieval=retrieval,
    )
    selection_result = select_editing_assignment(ctx=ctx, agent_context=agent_context)
    huazi_result = plan_huazi_overlays(
        ctx=ctx,
        agent_context=agent_context,
        selection_result=selection_result,
    )
    materialized = materialize_editing_outputs(
        request=state.request,
        node_id=ctx.node_run.node_id,
        agent_context=agent_context,
        selection_result=selection_result,
        huazi_result=huazi_result,
    )

    return NodeOutput(
        status=NodeStatus.degraded if materialized.degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_media_assignment,
                materialized.assignment_payload,
                "MediaAssignmentPlan.v1",
            ),
            ctx.artifact(
                ArtifactKind.plan_portrait,
                materialized.portrait_payload,
                "PortraitPlanArtifact.v1",
            ),
            ctx.artifact(ArtifactKind.plan_broll, materialized.broll_payload, "BrollPlanArtifact.v1"),
            ctx.artifact(ArtifactKind.plan_style, materialized.style_payload, "StylePlanArtifact.v1"),
            ctx.artifact(
                ArtifactKind.plan_editing_diagnostics,
                materialized.diagnostics,
                "EditingAgentDiagnostics.v1",
            ),
        ],
        warnings=materialized.warnings,
        degradations=materialized.degradations,
        provider_invocation_ids=[
            *selection_result.provider_invocation_ids,
            *huazi_result.provider_invocation_ids,
        ],
    )
