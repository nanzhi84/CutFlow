"""Active v2 media-selection orchestration.

This module owns only portrait/B-roll candidate selection and local
materialization. Post-process candidates and decisions never enter its types.
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
    WarningCode,
    utcnow,
)
from packages.core.contracts.artifacts import MediaSelectionAssignmentPlan
from packages.core.workflow import NodeExecutionError
from packages.planning.editing.frame_grid import frame_index
from packages.planning.material import longest_clean_portrait_source_span, shortlist_for_windows
from packages.production.pipeline._materialize import (
    full_coverage_broll_coverage_gaps,
    materialize_broll_from_assignment,
    materialize_full_coverage_broll_from_assignment,
    materialize_portrait_from_assignment,
)
from packages.production.pipeline._media_selection_agent import (
    MediaCandidates,
    MediaSelection,
    build_media_agent_input,
    deterministic_media_selection,
    index_media_candidates,
    select_media_with_repair,
    validate_media_selection,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline.nodes._broll_policy import broll_full_coverage_enabled

_JSON_VARS = frozenset({"narration_units", "safe_cut_boundaries", "portrait_slots", "broll_slots"})
_PROMPT_MAX_RETRIEVAL_CANDIDATES = 12
_PROMPT_MAX_OPTIONS_PER_SLOT = 6
_MEDIA_SELECTION_GENERATION_OPTIONS = {
    "response_format": {"type": "json_object"},
    "temperature": 0.1,
    "enable_thinking": False,
}


@dataclass(frozen=True)
class MediaSelectionContext:
    windows: dict
    raw_units: list[dict]
    duration: float
    media_boundary: dict
    shortlist_counts: dict
    candidates: MediaCandidates
    retrieval_topk_by_window: dict[str, list[str]]
    agent_input: dict
    prompt_input: dict
    prompt_domain_diagnostics: dict


@dataclass(frozen=True)
class MediaSelectionResult:
    selection: MediaSelection
    engine: str
    fallback_used: bool
    fallback_reason: str | None
    repair_trace: list[dict]
    warnings: list[WarningCode]
    degradations: list[DegradationNotice]
    provider_invocation_ids: list[str]


@dataclass(frozen=True)
class MediaSelectionMaterializedOutputs:
    assignment_payload: dict
    portrait_payload: dict
    broll_payload: dict
    diagnostics: dict
    warnings: list[WarningCode]
    degradations: list[DegradationNotice]


def build_media_selection_context(
    *,
    request,
    material: dict,
    narration: dict,
    boundary: dict,
    windows: dict,
    retrieval: dict | None = None,
) -> MediaSelectionContext:
    raw_units = [item for item in narration.get("units", []) or [] if isinstance(item, dict)]
    duration = max([float(unit.get("end", 0) or 0) for unit in raw_units] or [1.0])
    media_boundary = _boundary_with_compiled_windows(boundary, windows)
    shortlisted, shortlist_counts = shortlist_for_windows(
        windows.get("portrait_windows", []) or [],
        windows.get("broll_windows", []) or [],
        material,
    )
    shortlisted_media = _media_material(shortlisted)
    retrieval_topk = _retrieval_topk_by_window(retrieval)
    if retrieval is not None:
        for slot in [
            *(media_boundary.get("portrait_slots") or []),
            *(media_boundary.get("broll_slots") or []),
        ]:
            if isinstance(slot, dict) and str(slot.get("slot_id") or ""):
                retrieval_topk.setdefault(str(slot["slot_id"]), [])

    candidate_material = _media_material(material) if retrieval is not None else shortlisted_media
    candidates = index_media_candidates(candidate_material)
    prompt_candidates = (
        _prompt_candidates_for_retrieval(candidates, retrieval_topk)
        if retrieval is not None
        else candidates
    )
    agent_input = build_media_agent_input(
        request=request,
        boundary=media_boundary,
        candidates=prompt_candidates,
        narration_units=raw_units,
        duration=duration,
        retrieval_topk_by_window=retrieval_topk,
    )
    failure = _portrait_feasibility_failure(agent_input)
    if failure is not None:
        failure.update(_raw_portrait_candidate_diagnostics(_media_material(material)))
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "人像素材不足：portrait slot 没有可覆盖的源窗口。",
            details=failure,
        )
    full_coverage = broll_full_coverage_enabled(request)
    prompt_input, prompt_domain_diagnostics = _compact_prompt_input(
        agent_input,
        allow_broll_asset_diversity_reuse=full_coverage,
    )
    unmatched_portrait = prompt_domain_diagnostics["portrait"]["unmatched_slot_ids"]
    if unmatched_portrait:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "人像素材不足：portrait slot 无法形成 asset_id 全局唯一的完整指派。",
            details={
                "failed_slot_ids": unmatched_portrait,
                "prompt_candidate_domains": prompt_domain_diagnostics,
            },
        )
    unmatched_broll = prompt_domain_diagnostics["broll"]["unmatched_slot_ids"]
    if full_coverage and unmatched_broll:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_broll,
            "B-roll 素材不足：full_coverage slot 无法形成 candidate_id 全局唯一的完整指派。",
            details={
                "failed_slot_ids": unmatched_broll,
                "prompt_candidate_domains": prompt_domain_diagnostics,
            },
        )
    return MediaSelectionContext(
        windows=windows,
        raw_units=raw_units,
        duration=duration,
        media_boundary=media_boundary,
        shortlist_counts=shortlist_counts,
        candidates=candidates,
        retrieval_topk_by_window=retrieval_topk,
        agent_input=agent_input,
        prompt_input=prompt_input,
        prompt_domain_diagnostics=prompt_domain_diagnostics,
    )


def select_media_assignment(
    *,
    ctx: NodeContext,
    agent_context: MediaSelectionContext,
) -> MediaSelectionResult:
    state = ctx.state
    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    warnings: list[WarningCode] = []
    degradations: list[DegradationNotice] = []
    invocation_ids: list[str] = []
    repair_trace: list[dict] = []
    fallback_used = False
    fallback_reason: str | None = None
    full_coverage = broll_full_coverage_enabled(state.request)
    broll_limit = _broll_assignment_limit(request=state.request, windows=agent_context.windows)

    def _validated_fallback() -> MediaSelection:
        fallback = deterministic_media_selection(
            boundary=agent_context.media_boundary,
            candidates=agent_context.candidates,
            max_inserts=broll_limit,
            retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=full_coverage,
        )
        errors = validate_media_selection(
            fallback,
            boundary=agent_context.media_boundary,
            candidates=agent_context.candidates,
            max_inserts=broll_limit,
            retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=full_coverage,
            require_broll_coverage=full_coverage,
        )
        if errors:
            code = (
                ErrorCode.material_insufficient_portrait
                if any(error.startswith("portrait slots not covered:") for error in errors)
                else ErrorCode.material_insufficient_broll
                if any(error.startswith("broll slots not covered:") for error in errors)
                else ErrorCode.prompt_output_invalid
            )
            raise NodeExecutionError(
                code,
                "确定性兜底选择无法满足媒体窗口约束：" + "；".join(errors[:5]),
                details={"errors": errors},
            )
        return fallback

    if profile is None:
        if not sandbox_fallback_allowed():
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "未配置可用的真实 LLM 供应商（llm.chat）。"
                "请在「设置」中配置并启用真实 LLM 供应商及密钥。",
            )
        selection = _validated_fallback()
        engine = "deterministic_fallback"
        fallback_used = True
        fallback_reason = "no_provider"
        warnings.append(WarningCode.media_selection_agent_deterministic_fallback)
        degradations.append(
            degradation_notice(
                WarningCode.media_selection_agent_deterministic_fallback,
                "媒体选择 Agent 无可用真实 LLM 供应商，改用确定性兜底选择。",
                node_id=ctx.node_run.node_id,
                affects_true_yield=False,
            )
        )
    else:
        engine = "media_selection_agent_llm"
        prompt_input = agent_context.prompt_input

        def _invoke(previous_errors: list[str]):
            attempt = len(invocation_ids)
            prompt_invocation, rendered = ctx.prompt_registry.render(
                node_id="MediaSelectionAgentPlanning",
                variables=_prompt_variables(prompt_input, previous_errors),
                case_id=ctx.run.case_id,
                run_id=ctx.run.id,
                node_run_id=ctx.node_run.id,
                provider_profile_id=profile.id,
            )
            request_artifact = _record_request(
                ctx, profile, prompt_invocation, rendered, attempt, previous_errors
            )
            idempotency = ctx.provider_call_idempotency(
                logical_call_slot=f"media_selection_agent:attempt-{attempt}",
                provider_profile_id=profile.id,
            )
            invocation, result = ctx.provider_gateway.invoke(
                ProviderCall(
                    case_id=ctx.run.case_id,
                    run_id=ctx.run.id,
                    node_run_id=ctx.node_run.id,
                    provider_profile_id=profile.id,
                    capability_id="llm.chat",
                    prompt_version_id=prompt_invocation.prompt_version_id,
                    input={
                        "prompt": rendered,
                        **_MEDIA_SELECTION_GENERATION_OPTIONS,
                    },
                    idempotency_key=idempotency.key,
                    fallback_idempotency_keys=idempotency.fallback_keys,
                )
            )
            response_artifact = _record_response(ctx, invocation, result, attempt)
            _attach_provider_artifacts(ctx, invocation.id, request_artifact, response_artifact)
            if result is None or invocation.error:
                raise NodeExecutionError(
                    invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                    invocation.error.message
                    if invocation.error
                    else "Media selection provider failed.",
                    retryable=True,
                )
            invocation_ids.append(invocation.id)
            ctx.prompt_registry.validate_output(
                prompt_version_id=prompt_invocation.prompt_version_id,
                output=result.output,
            )
            return _unwrap_provider_selection(result.output)

        selection, repair_trace, errors = select_media_with_repair(
            invoke=_invoke,
            boundary=agent_context.media_boundary,
            candidates=agent_context.candidates,
            max_inserts=broll_limit,
            max_repair_attempts=state.request.edit.max_repair_attempts,
            retrieval_topk_by_window=agent_context.retrieval_topk_by_window,
            allow_broll_asset_diversity_reuse=full_coverage,
            require_broll_coverage=full_coverage,
        )
        llm_repair_used = any(
            isinstance(item.get("attempt"), int) and int(item["attempt"]) > 0
            for item in repair_trace
            if isinstance(item, dict)
        )
        local_repair_used = bool(
            repair_trace
            and repair_trace[-1].get("attempt") == "local_media_constraint_repair"
            and int(repair_trace[-1].get("error_count") or 0) == 0
        )
        if local_repair_used:
            warnings.append(WarningCode.media_selection_agent_local_constraint_repair)
        if errors:
            if not sandbox_fallback_allowed():
                raise NodeExecutionError(
                    ErrorCode.prompt_output_invalid,
                    f"媒体选择 Agent 的输出在 {state.request.edit.max_repair_attempts} "
                    "次修复后仍不合法：" + "；".join(errors[:5]),
                )
            selection = _validated_fallback()
            engine = "deterministic_fallback"
            fallback_used = True
            fallback_reason = "llm_unrepairable"
            warnings.append(WarningCode.media_selection_agent_deterministic_fallback)
            degradations.append(
                degradation_notice(
                    WarningCode.media_selection_agent_deterministic_fallback,
                    "媒体选择 Agent 输出不可修复，改用确定性兜底选择。",
                    node_id=ctx.node_run.node_id,
                    affects_true_yield=False,
                )
            )
        elif llm_repair_used:
            warnings.append(WarningCode.media_selection_agent_llm_repair)

    return MediaSelectionResult(
        selection=selection,
        engine=engine,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        repair_trace=repair_trace,
        warnings=list(dict.fromkeys(warnings)),
        degradations=degradations,
        provider_invocation_ids=invocation_ids,
    )


def materialize_media_selection_outputs(
    *,
    request,
    node_id: str,
    agent_context: MediaSelectionContext,
    selection_result: MediaSelectionResult,
) -> MediaSelectionMaterializedOutputs:
    selection = selection_result.selection
    candidates = agent_context.candidates
    windows = agent_context.windows
    warnings = list(selection_result.warnings)
    degradations = list(selection_result.degradations)
    use_default_portrait = (
        selection_result.fallback_used and not agent_context.retrieval_topk_by_window
    )
    portrait_assignment = (
        _default_portrait_assignment(windows)
        if use_default_portrait
        else [
            {
                "window_id": choice.slot_id,
                "candidate_id": choice.candidate_id,
                "source_mode": "lipsynced",
                "reason": choice.reason,
            }
            for choice in selection.portrait
        ]
    )
    broll_assignment = [
        {
            "window_id": choice.slot_id,
            "candidate_id": choice.candidate_id,
            "reason": choice.reason,
            "confidence": 0.5,
            "matched_keywords": list(
                _candidate_metadata(candidates.broll_by_id.get(choice.candidate_id)).get(
                    "matched_keywords"
                )
                or []
            ),
        }
        for choice in selection.broll
    ]
    assignment = {"portrait": portrait_assignment, "broll": broll_assignment}
    portrait_payload = (
        _default_portrait_payload(windows)
        if use_default_portrait
        else materialize_portrait_from_assignment(
            windows=windows,
            assignment=assignment,
            candidates=candidates,
        )
    )
    broll_limit = _broll_assignment_limit(request=request, windows=windows)
    if broll_full_coverage_enabled(request):
        broll_payload, broll_drops = materialize_full_coverage_broll_from_assignment(
            windows=windows,
            assignment=assignment,
            candidates=candidates,
            enabled=request.broll.enabled,
            max_inserts=broll_limit,
        )
        _ensure_full_coverage_broll(
            windows=windows,
            broll_payload=broll_payload,
            broll_drops=broll_drops,
        )
    else:
        broll_payload, broll_drops = materialize_broll_from_assignment(
            windows=windows,
            assignment=assignment,
            candidates=candidates,
            enabled=request.broll.enabled,
            max_inserts=broll_limit,
        )
    if broll_drops:
        degradations.append(
            degradation_notice(
                WarningCode.broll_insertions_dropped_geometry,
                f"B-roll 有 {len(broll_drops)} 个插入因时间线几何约束被丢弃。",
                node_id=node_id,
                affects_true_yield=bool(selection.broll) and not broll_payload.get("overlays"),
            ).model_copy(update={"details": {"broll_drops": broll_drops}})
        )
        warnings.append(WarningCode.broll_insertions_dropped_geometry)

    assignment_diagnostics = {
        "repair_trace": selection_result.repair_trace,
        "shortlist_counts": agent_context.shortlist_counts,
        "retrieval_topk_by_window": agent_context.retrieval_topk_by_window,
        "fallback_used": selection_result.fallback_used,
        "fallback_reason": selection_result.fallback_reason,
        "broll_drops": broll_drops,
        "prompt_candidate_domains": agent_context.prompt_domain_diagnostics,
    }
    assignment_payload = MediaSelectionAssignmentPlan(
        engine=selection_result.engine,
        portrait=portrait_assignment,
        broll=broll_assignment,
        diagnostics=assignment_diagnostics,
    ).model_dump(mode="json")
    diagnostics = {
        "mode": selection_result.engine,
        "instruction": request.edit.instruction,
        "analysis": selection.analysis,
        "repair_trace": selection_result.repair_trace,
        "portrait_choices": [
            {
                "slot_id": item["window_id"],
                "candidate_id": item["candidate_id"],
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
        "shortlist_counts": agent_context.shortlist_counts,
        "retrieval_topk_by_window": agent_context.retrieval_topk_by_window,
        "fallback_used": selection_result.fallback_used,
        "fallback_reason": selection_result.fallback_reason,
        "prompt_candidate_domains": agent_context.prompt_domain_diagnostics,
        "candidate_counts": {
            "portrait": len(candidates.portrait_by_id),
            "broll": len(candidates.broll_by_id),
        },
    }
    return MediaSelectionMaterializedOutputs(
        assignment_payload=assignment_payload,
        portrait_payload=portrait_payload,
        broll_payload=broll_payload,
        diagnostics=diagnostics,
        warnings=list(dict.fromkeys(warnings)),
        degradations=degradations,
    )


def _media_material(material: dict) -> dict:
    return {
        "portrait_candidates": list(material.get("portrait_candidates") or []),
        "broll_candidates": list(material.get("broll_candidates") or []),
    }


def _boundary_with_compiled_windows(boundary: dict, windows: dict) -> dict:
    return {
        "safe_cut_boundaries": list(boundary.get("safe_cut_boundaries") or []),
        "portrait_slots": [
            {
                "slot_id": str(window.get("window_id") or ""),
                "start_frame": int(window.get("start_frame", 0) or 0),
                "end_frame": int(window.get("end_frame", 0) or 0),
                "unit_ids": list(window.get("unit_ids") or []),
            }
            for window in windows.get("portrait_windows") or []
            if isinstance(window, dict)
        ],
        "broll_slots": [
            {
                "slot_id": str(window.get("window_id") or ""),
                "start_frame": int(window.get("start_frame", 0) or 0),
                "end_frame": int(window.get("end_frame", 0) or 0),
                "source_length_frames": int(
                    window.get("source_length_frames")
                    or max(
                        0,
                        int(window.get("end_frame", 0) or 0)
                        - int(window.get("start_frame", 0) or 0),
                    )
                ),
                "unit_ids": list(window.get("host_unit_ids") or window.get("unit_ids") or []),
                "text": str(window.get("text") or ""),
            }
            for window in windows.get("broll_windows") or []
            if isinstance(window, dict)
        ],
    }


def _retrieval_topk_by_window(retrieval: dict | None) -> dict[str, list[str]]:
    values = retrieval.get("candidates_by_window") if isinstance(retrieval, dict) else {}
    if not isinstance(values, dict):
        return {}
    return {
        str(window_id): [
            str(item.get("candidate_id") or "")
            for item in candidates
            if isinstance(item, dict) and str(item.get("candidate_id") or "")
        ]
        for window_id, candidates in values.items()
        if isinstance(candidates, list)
    }


def _prompt_candidates_for_retrieval(
    candidates: MediaCandidates,
    retrieval_topk: dict[str, list[str]],
) -> MediaCandidates:
    allowed = {candidate_id for values in retrieval_topk.values() for candidate_id in values}
    return MediaCandidates(
        portrait_by_id={
            key: value for key, value in candidates.portrait_by_id.items() if key in allowed
        },
        broll_by_id={key: value for key, value in candidates.broll_by_id.items() if key in allowed},
    )


def _portrait_feasibility_failure(agent_input: dict) -> dict | None:
    failed: list[str] = []
    required: dict[str, int] = {}
    for slot in agent_input.get("portrait_slots") or []:
        if not isinstance(slot, dict):
            continue
        legal = set(slot.get("legal_candidate_ids") or [])
        if "retrieval_topk_candidate_ids" in slot:
            legal &= set(slot.get("retrieval_topk_candidate_ids") or [])
        if not legal and str(slot.get("slot_id") or ""):
            slot_id = str(slot["slot_id"])
            failed.append(slot_id)
            required[slot_id] = int(slot.get("required_frames", 0) or 0)
    if not failed:
        return None
    return {"failed_slot_ids": failed, "required_frames_by_slot": required}


def _raw_portrait_candidate_diagnostics(material: dict) -> dict:
    candidates = index_media_candidates(material).portrait_by_id.values()
    available = []
    for candidate in candidates:
        span = longest_clean_portrait_source_span(_candidate_metadata(candidate))
        available.append(0 if span is None else frame_index(span[1]) - frame_index(span[0]))
    return {
        "longest_available_source_frames": max(available or [0]),
        "portrait_candidate_count": len(available),
    }


def _compact_prompt_input(
    agent_input: dict,
    *,
    allow_broll_asset_diversity_reuse: bool = False,
) -> tuple[dict, dict]:
    """Compile slot-scoped candidate domains that are safe by construction.

    The provider previously had to join global candidate tables against per-slot ID
    arrays and then solve global asset/diversity conflicts itself. Real runs showed
    that this cross-table task was the dominant source of invalid first responses.
    Here every prompt candidate is embedded in its slot. Coverage witnesses are
    computed from each complete legal domain before prompt-size pruning, and every
    cross-slot choice is compatible by construction.
    """

    portrait_candidates = _candidate_input_by_id(agent_input, "portrait_candidates")
    broll_candidates = _candidate_input_by_id(agent_input, "broll_candidates")
    raw_portrait_slots = _prompt_slot_candidates(agent_input.get("portrait_slots") or [])
    raw_broll_slots = _prompt_slot_candidates(agent_input.get("broll_slots") or [])
    portrait_domains, unmatched_portrait = _partition_portrait_prompt_domains(
        raw_portrait_slots,
        portrait_candidates,
    )
    broll_domains, unmatched_broll, broll_partition_diagnostics = _partition_broll_prompt_domains(
        raw_broll_slots,
        broll_candidates,
        max_inserts=max(0, int(agent_input.get("max_broll_inserts", 0) or 0)),
        allow_asset_diversity_reuse=allow_broll_asset_diversity_reuse,
    )

    portrait_slots = [
        {
            "slot_id": slot_id,
            "required_seconds": slot["required_seconds"],
            "legal_candidates": [
                _portrait_prompt_candidate(portrait_candidates[candidate_id])
                for candidate_id in portrait_domains.get(slot_id, [])
            ],
        }
        for slot_id, slot in raw_portrait_slots.items()
    ]
    broll_slots = [
        {
            "slot_id": slot_id,
            "required_seconds": slot["required_seconds"],
            "text": slot["text"],
            "conflicts_with_slot_ids": slot["conflicts_with_slot_ids"],
            "legal_candidates": [
                _broll_prompt_candidate(broll_candidates[candidate_id])
                for candidate_id in broll_domains.get(slot_id, [])
            ],
        }
        for slot_id, slot in raw_broll_slots.items()
    ]
    prompt_input = {
        key: value
        for key, value in agent_input.items()
        if key not in {"portrait_candidates", "broll_candidates"}
    }
    prompt_input.update({"portrait_slots": portrait_slots, "broll_slots": broll_slots})
    diagnostics = {
        "strategy": "slot_scoped_direct_compatibility_v2",
        "portrait": _prompt_domain_diagnostics(
            raw_portrait_slots,
            portrait_domains,
            unmatched_portrait,
        ),
        "broll": {
            **_prompt_domain_diagnostics(raw_broll_slots, broll_domains, unmatched_broll),
            **broll_partition_diagnostics,
            "asset_diversity_reuse_allowed": allow_broll_asset_diversity_reuse,
        },
    }
    diagnostics["prompt_json_chars"] = len(json.dumps(prompt_input, ensure_ascii=False))
    return prompt_input, diagnostics


def _candidate_input_by_id(agent_input: dict, key: str) -> dict[str, dict]:
    return {
        str(item.get("candidate_id") or ""): item
        for item in agent_input.get(key) or []
        if isinstance(item, dict) and str(item.get("candidate_id") or "")
    }


def _prompt_slot_candidates(raw_slots) -> dict[str, dict]:
    slots: dict[str, dict] = {}
    for slot in raw_slots:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id") or "")
        if not slot_id:
            continue
        legal = [
            value
            for raw_value in slot.get("legal_candidate_ids") or []
            if (value := str(raw_value).strip())
        ]
        retrieval_constrained = "retrieval_topk_candidate_ids" in slot
        if "retrieval_topk_candidate_ids" in slot:
            legal_set = set(legal)
            legal = [
                value
                for raw_value in slot.get("retrieval_topk_candidate_ids") or []
                if (value := str(raw_value).strip()) and value in legal_set
            ]
        slots[slot_id] = {
            "required_seconds": slot.get("required_seconds"),
            "text": str(slot.get("text") or ""),
            "conflicts_with_slot_ids": list(slot.get("conflicts_with_slot_ids") or []),
            "candidate_ids": list(dict.fromkeys(legal)),
            "retrieval_constrained": retrieval_constrained,
        }
    return slots


def _partition_portrait_prompt_domains(
    slots: dict[str, dict],
    candidates: dict[str, dict],
) -> tuple[dict[str, list[str]], list[str]]:
    best_by_slot_asset: dict[str, dict[str, tuple[int, str]]] = {}
    for slot_id, slot in slots.items():
        options: dict[str, tuple[int, str]] = {}
        for rank, candidate_id in enumerate(slot["candidate_ids"]):
            candidate = candidates.get(candidate_id)
            if candidate is None:
                continue
            asset_id = str(candidate.get("asset_id") or candidate_id)
            options.setdefault(asset_id, (rank, candidate_id))
        best_by_slot_asset[slot_id] = options

    assignment = _maximum_slot_matching(
        {
            slot_id: [
                asset_id
                for asset_id, _ in sorted(
                    options.items(),
                    key=lambda item: (item[1][0], item[0]),
                )
            ]
            for slot_id, options in best_by_slot_asset.items()
        }
    )
    domains: dict[str, list[tuple[int, str]]] = {slot_id: [] for slot_id in slots}
    assigned_assets = set(assignment.values())
    for slot_id, asset_id in assignment.items():
        domains[slot_id].append(best_by_slot_asset[slot_id][asset_id])

    all_assets = sorted(
        {asset_id for options in best_by_slot_asset.values() for asset_id in options},
        key=lambda asset_id: (
            sum(asset_id in options for options in best_by_slot_asset.values()),
            min(
                options[asset_id][0]
                for options in best_by_slot_asset.values()
                if asset_id in options
            ),
            asset_id,
        ),
    )
    for asset_id in all_assets:
        if asset_id in assigned_assets:
            continue
        options = [
            (len(domains[slot_id]), rank, slot_id, candidate_id)
            for slot_id, values in best_by_slot_asset.items()
            if asset_id in values
            for rank, candidate_id in [values[asset_id]]
            if len(domains[slot_id]) < _PROMPT_MAX_OPTIONS_PER_SLOT
            and rank < _PROMPT_MAX_RETRIEVAL_CANDIDATES
        ]
        if not options:
            continue
        _, rank, slot_id, candidate_id = min(options)
        domains[slot_id].append((rank, candidate_id))
        assigned_assets.add(asset_id)

    return (
        {
            slot_id: [candidate_id for _, candidate_id in sorted(values)]
            for slot_id, values in domains.items()
        },
        sorted(set(slots) - set(assignment)),
    )


def _partition_broll_prompt_domains(
    slots: dict[str, dict],
    candidates: dict[str, dict],
    *,
    max_inserts: int,
    allow_asset_diversity_reuse: bool,
) -> tuple[dict[str, list[str]], list[str], dict]:
    if allow_asset_diversity_reuse:
        return _partition_full_coverage_broll_domains(
            slots,
            candidates,
            max_inserts=max_inserts,
        )
    return _partition_insert_broll_domains(
        slots,
        candidates,
        max_inserts=max_inserts,
    )


def _partition_full_coverage_broll_domains(
    slots: dict[str, dict],
    candidates: dict[str, dict],
    *,
    max_inserts: int,
) -> tuple[dict[str, list[str]], list[str], dict]:
    options_by_slot = {
        slot_id: [
            candidate_id for candidate_id in slot["candidate_ids"] if candidate_id in candidates
        ]
        for slot_id, slot in slots.items()
    }
    assignment = _maximum_slot_matching(options_by_slot)
    ranked_domains: dict[str, list[tuple[int, str]]] = {slot_id: [] for slot_id in slots}
    assigned_candidate_ids = set(assignment.values())
    rank_by_slot = {
        slot_id: {candidate_id: rank for rank, candidate_id in enumerate(candidate_ids)}
        for slot_id, candidate_ids in options_by_slot.items()
    }
    for slot_id, candidate_id in assignment.items():
        ranked_domains[slot_id].append((rank_by_slot[slot_id][candidate_id], candidate_id))

    candidate_ids = sorted(
        {candidate_id for values in options_by_slot.values() for candidate_id in values},
        key=lambda candidate_id: (
            sum(candidate_id in values for values in options_by_slot.values()),
            min(
                rank_by_slot[slot_id][candidate_id]
                for slot_id, values in options_by_slot.items()
                if candidate_id in values
            ),
            candidate_id,
        ),
    )
    for candidate_id in candidate_ids:
        if candidate_id in assigned_candidate_ids:
            continue
        destinations = [
            (len(ranked_domains[slot_id]), rank_by_slot[slot_id][candidate_id], slot_id)
            for slot_id, values in options_by_slot.items()
            if candidate_id in values
            and rank_by_slot[slot_id][candidate_id] < _PROMPT_MAX_RETRIEVAL_CANDIDATES
            and len(ranked_domains[slot_id]) < _PROMPT_MAX_OPTIONS_PER_SLOT
        ]
        if not destinations:
            continue
        _, rank, slot_id = min(destinations)
        ranked_domains[slot_id].append((rank, candidate_id))
        assigned_candidate_ids.add(candidate_id)

    overlapping_slot_ids = sorted(
        {
            slot_id
            for slot_id, slot in slots.items()
            if any(conflict_id in slots for conflict_id in slot["conflicts_with_slot_ids"])
        }
    )
    max_inserts_insufficient = max_inserts < len(slots)
    unmatched = sorted(
        (set(slots) - set(assignment))
        | set(overlapping_slot_ids)
        | (set(slots) if max_inserts_insufficient else set())
    )
    domains = {
        slot_id: [candidate_id for _, candidate_id in sorted(values)]
        for slot_id, values in ranked_domains.items()
    }
    return (
        domains,
        unmatched,
        {
            "mode": "full_coverage",
            "construction_safe_max_inserts": not max_inserts_insufficient,
            "construction_safe_timeline_overlap": not overlapping_slot_ids,
            "max_inserts": max_inserts,
            "max_inserts_insufficient": max_inserts_insufficient,
            "coverage_witness_count": len(assignment),
            "witness_beyond_display_cutoff": sum(
                rank_by_slot[slot_id][candidate_id] >= _PROMPT_MAX_RETRIEVAL_CANDIDATES
                for slot_id, candidate_id in assignment.items()
            ),
            "overlapping_slot_ids": overlapping_slot_ids,
        },
    )


def _partition_insert_broll_domains(
    slots: dict[str, dict],
    candidates: dict[str, dict],
    *,
    max_inserts: int,
) -> tuple[dict[str, list[str]], list[str], dict]:
    domains: dict[str, list[str]] = {slot_id: [] for slot_id in slots}
    selected, witness_diagnostics = _find_insert_broll_witness(
        slots,
        candidates,
        max_inserts=max_inserts,
    )
    direct_conflict_pruned = 0
    for slot_id, candidate_id in selected.items():
        domains[slot_id].append(candidate_id)

    for slot_id in selected:
        for rank, candidate_id in enumerate(slots[slot_id]["candidate_ids"]):
            if rank >= _PROMPT_MAX_RETRIEVAL_CANDIDATES:
                break
            candidate = candidates.get(candidate_id)
            if (
                candidate is None
                or candidate_id in domains[slot_id]
                or len(domains[slot_id]) >= _PROMPT_MAX_OPTIONS_PER_SLOT
            ):
                continue
            compatible = all(
                not _broll_candidates_conflict(candidate, candidates[other_id])
                for other_slot_id, other_domain in domains.items()
                if other_slot_id != slot_id
                for other_id in other_domain
            )
            if compatible:
                domains[slot_id].append(candidate_id)
            else:
                direct_conflict_pruned += 1

    unmatched = sorted(slot_id for slot_id, values in domains.items() if not values)
    input_occurrences = sum(len(slot["candidate_ids"]) for slot in slots.values())
    prompt_occurrences = sum(len(values) for values in domains.values())
    overlap_pruned_slots = {
        slot_id
        for slot_id, slot in slots.items()
        if not domains[slot_id] and set(slot["conflicts_with_slot_ids"]) & set(selected)
    }
    return (
        domains,
        unmatched,
        {
            "mode": "insert",
            "construction_safe_max_inserts": len(selected) <= max_inserts,
            "construction_safe_timeline_overlap": not any(
                set(slots[slot_id]["conflicts_with_slot_ids"]) & set(selected)
                for slot_id in selected
            ),
            "selected_slot_count": len(selected),
            "max_inserts": max_inserts,
            "overlap_pruned_slot_ids": sorted(overlap_pruned_slots),
            "direct_conflict_pruned_occurrences": direct_conflict_pruned,
            "pruned_candidate_occurrences": max(0, input_occurrences - prompt_occurrences),
            **witness_diagnostics,
        },
    )


def _find_insert_broll_witness(
    slots: dict[str, dict],
    candidates: dict[str, dict],
    *,
    max_inserts: int,
) -> tuple[dict[str, str], dict]:
    """Find the largest compatible slot/candidate witness within a fixed budget."""

    ordered_slot_ids = sorted(
        slots,
        key=lambda slot_id: (
            len(
                [
                    candidate_id
                    for candidate_id in slots[slot_id]["candidate_ids"]
                    if candidate_id in candidates
                ]
            ),
            len(slots[slot_id]["conflicts_with_slot_ids"]),
            slot_id,
        ),
    )
    target = min(
        max(0, max_inserts),
        sum(
            any(candidate_id in candidates for candidate_id in slots[slot_id]["candidate_ids"])
            for slot_id in ordered_slot_ids
        ),
    )
    search_budget = 100_000
    search_nodes = 0
    direct_conflict_rejections = 0
    exhausted = False
    best: dict[str, str] = {}
    solution: dict[str, str] = {}

    def _search(start: int, desired_count: int, current: dict[str, str]) -> bool:
        nonlocal search_nodes, direct_conflict_rejections, exhausted, best, solution
        if len(current) > len(best):
            best = dict(current)
        if len(current) == desired_count:
            solution = dict(current)
            return True
        if len(current) + len(ordered_slot_ids) - start < desired_count:
            return False
        for position in range(start, len(ordered_slot_ids)):
            if exhausted:
                return False
            slot_id = ordered_slot_ids[position]
            if set(slots[slot_id]["conflicts_with_slot_ids"]) & set(current):
                continue
            for candidate_id in slots[slot_id]["candidate_ids"]:
                candidate = candidates.get(candidate_id)
                if candidate is None:
                    continue
                search_nodes += 1
                if search_nodes > search_budget:
                    exhausted = True
                    return False
                if any(
                    _broll_candidates_conflict(candidate, candidates[other_id])
                    for other_id in current.values()
                ):
                    direct_conflict_rejections += 1
                    continue
                current[slot_id] = candidate_id
                if _search(position + 1, desired_count, current):
                    return True
                current.pop(slot_id)
        return False

    for desired_count in range(target, 0, -1):
        if _search(0, desired_count, {}):
            break
        if exhausted:
            break
    selected = solution or best
    return selected, {
        "witness_search_nodes": search_nodes,
        "witness_search_budget": search_budget,
        "witness_search_exhausted": exhausted,
        "witness_direct_conflict_rejections": direct_conflict_rejections,
    }


def _broll_candidates_conflict(left: dict, right: dict) -> bool:
    left_id = str(left.get("candidate_id") or "")
    right_id = str(right.get("candidate_id") or "")
    if left_id and left_id == right_id:
        return True
    left_asset = str(left.get("asset_id") or "")
    right_asset = str(right.get("asset_id") or "")
    if left_asset and left_asset == right_asset:
        return True
    left_diversity = str(left.get("diversity_key") or "")
    right_diversity = str(right.get("diversity_key") or "")
    return bool(left_diversity and left_diversity == right_diversity)


def _maximum_slot_matching(options_by_slot: dict[str, list[str]]) -> dict[str, str]:
    owner_by_option: dict[str, str] = {}
    option_by_slot: dict[str, str] = {}

    def _assign(slot_id: str, seen: set[str]) -> bool:
        for option_id in options_by_slot.get(slot_id, []):
            if option_id in seen:
                continue
            seen.add(option_id)
            owner = owner_by_option.get(option_id)
            if owner is None or _assign(owner, seen):
                owner_by_option[option_id] = slot_id
                option_by_slot[slot_id] = option_id
                return True
        return False

    for slot_id in sorted(options_by_slot, key=lambda value: (len(options_by_slot[value]), value)):
        _assign(slot_id, set())
    return option_by_slot


def _portrait_prompt_candidate(candidate: dict) -> dict:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "asset_id": candidate.get("asset_id"),
        "available_seconds": candidate.get("available_seconds"),
        "description": candidate.get("description"),
        "reason": candidate.get("reason"),
    }


def _broll_prompt_candidate(candidate: dict) -> dict:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "asset_id": candidate.get("asset_id"),
        "diversity_key": candidate.get("diversity_key"),
        "scene_name": candidate.get("scene_name"),
        "matched_keywords": list(candidate.get("matched_keywords") or [])[:6],
        "available_seconds": candidate.get("available_seconds"),
        "description": candidate.get("description"),
    }


def _prompt_domain_diagnostics(
    raw_slots: dict[str, dict],
    domains: dict[str, list[str]],
    unmatched_slot_ids: list[str],
) -> dict:
    input_occurrences = sum(len(slot["candidate_ids"]) for slot in raw_slots.values())
    prompt_occurrences = sum(len(values) for values in domains.values())
    rank_by_slot = {
        slot_id: {candidate_id: rank for rank, candidate_id in enumerate(slot["candidate_ids"])}
        for slot_id, slot in raw_slots.items()
    }
    return {
        "slot_count": len(raw_slots),
        "input_candidate_occurrences": input_occurrences,
        "prompt_candidate_count": prompt_occurrences,
        "pruned_candidate_occurrences": max(0, input_occurrences - prompt_occurrences),
        "beyond_display_cutoff_count": sum(
            rank_by_slot.get(slot_id, {}).get(candidate_id, -1) >= _PROMPT_MAX_RETRIEVAL_CANDIDATES
            for slot_id, candidate_ids in domains.items()
            for candidate_id in candidate_ids
        ),
        "options_by_slot": {slot_id: len(domains.get(slot_id, [])) for slot_id in raw_slots},
        "unmatched_slot_ids": unmatched_slot_ids,
    }


def _prompt_variables(agent_input: dict, errors: list[str]) -> dict:
    values = {
        key: json.dumps(value, ensure_ascii=False) if key in _JSON_VARS else str(value)
        for key, value in agent_input.items()
    }
    values["repair_feedback"] = (
        "上一轮选择存在以下问题，请只修正这些点后重新只输出 JSON：\n- " + "\n- ".join(errors)
        if errors
        else ""
    )
    return values


def _unwrap_provider_selection(output):
    """Accept a direct selection or the exact DashScope ``content/intent`` envelope."""

    if not isinstance(output, dict) or set(output) != {"content", "intent"}:
        return output
    if not isinstance(output.get("content"), str) or not isinstance(output.get("intent"), dict):
        return output
    return output["intent"]


def _record_request(ctx, profile, prompt_invocation, prompt, attempt, errors) -> Artifact:
    return ctx.artifact(
        ArtifactKind.provider_raw_request,
        {
            "capability_id": "llm.chat",
            "provider_profile_id": profile.id,
            "provider_id": getattr(profile, "provider_id", None),
            "model_id": getattr(profile, "model_id", None),
            "prompt_version_id": prompt_invocation.prompt_version_id,
            "prompt_invocation_id": prompt_invocation.id,
            "attempt": attempt,
            "repair_errors": list(errors),
            "generation_options": _MEDIA_SELECTION_GENERATION_OPTIONS,
            "prompt": prompt,
        },
        "MediaSelectionAgentLlmRequestSnapshot.v1",
    )


def _record_response(ctx, invocation, result, attempt) -> Artifact:
    error = getattr(invocation, "error", None)
    status = getattr(invocation, "status", "unknown")
    return ctx.artifact(
        ArtifactKind.provider_raw_response,
        {
            "capability_id": "llm.chat",
            "provider_invocation_id": invocation.id,
            "provider_profile_id": getattr(invocation, "provider_profile_id", None),
            "provider_id": getattr(invocation, "provider_id", None),
            "model_id": getattr(invocation, "model_id", None),
            "prompt_version_id": getattr(invocation, "prompt_version_id", None),
            "attempt": attempt,
            "status": status.value if hasattr(status, "value") else str(status),
            "error": error.model_dump(mode="json") if error else None,
            "output": result.output if result is not None else None,
        },
        "MediaSelectionAgentLlmResponseSnapshot.v1",
    )


def _attach_provider_artifacts(ctx, invocation_id, request_artifact, response_artifact) -> None:
    current = ctx.repository.provider_invocations.get(invocation_id)
    if current is not None:
        ctx.repository.provider_invocations[invocation_id] = current.model_copy(
            update={
                "request_artifact_id": request_artifact.id,
                "response_artifact_id": response_artifact.id,
                "updated_at": utcnow(),
            }
        )


def _default_portrait_assignment(windows: dict) -> list[dict]:
    defaults = (windows.get("default_assignment") or {}).get("portrait") or []
    portrait_windows = windows.get("portrait_windows") or []
    return [
        {
            "window_id": str(window.get("window_id") or ""),
            "candidate_id": str(default.get("window_id") or ""),
            "source_mode": str(
                (default.get("segment_payload") or {}).get("source_mode") or "lipsynced"
            ),
            "reason": "compiler default",
        }
        for window, default in zip(portrait_windows, defaults)
        if isinstance(window, dict) and isinstance(default, dict)
    ]


def _default_portrait_payload(windows: dict) -> dict:
    return dict((windows.get("default_assignment") or {}).get("portrait_plan_payload") or {})


def _candidate_metadata(candidate: dict | None) -> dict:
    value = candidate.get("metadata") if isinstance(candidate, dict) else None
    return value if isinstance(value, dict) else {}


def _broll_assignment_limit(*, request, windows: dict) -> int:
    if broll_full_coverage_enabled(request):
        return len([item for item in windows.get("broll_windows") or [] if isinstance(item, dict)])
    return request.broll.max_inserts


def _ensure_full_coverage_broll(
    *, windows: dict, broll_payload: dict, broll_drops: list[dict]
) -> None:
    expected = {
        str(window.get("window_id") or "")
        for window in windows.get("broll_windows") or []
        if isinstance(window, dict) and str(window.get("window_id") or "")
    }
    overlays = [item for item in broll_payload.get("overlays") or [] if isinstance(item, dict)]
    covered = {str(item.get("window_id") or "") for item in overlays}
    gaps = full_coverage_broll_coverage_gaps(windows=windows, overlays=overlays)
    missing = sorted(expected - covered)
    if missing or gaps or broll_drops:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_broll,
            "B-roll full coverage requires every authoritative window to have material.",
            details={
                "missing_broll_window_ids": missing,
                "expected_broll_window_count": len(expected),
                "covered_broll_window_count": len(covered),
                "coverage_gaps": gaps,
                "broll_drops": broll_drops,
            },
        )
