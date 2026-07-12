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
    portrait_cut_frames,
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
_PORTRAIT_CANDIDATE_HEADER = "candidate_id | asset_id | available_seconds | description | reason"
_BROLL_CANDIDATE_HEADER = (
    "candidate_id | asset_id | diversity_key | scene_name | allowed_slot_ids | matched_keywords | "
    "available_seconds | description"
)


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
    return MediaSelectionContext(
        windows=windows,
        raw_units=raw_units,
        duration=duration,
        media_boundary=media_boundary,
        shortlist_counts=shortlist_counts,
        candidates=candidates,
        retrieval_topk_by_window=retrieval_topk,
        agent_input=agent_input,
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
        prompt_input = _compact_prompt_input(agent_context.agent_input)

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
                    input={"prompt": rendered, "response_format": {"type": "json_object"}},
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
            cut_frames=portrait_cut_frames(portrait_payload),
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


def _compact_prompt_input(agent_input: dict) -> dict:
    portrait_slots = []
    for slot in agent_input.get("portrait_slots") or []:
        if not isinstance(slot, dict):
            continue
        legal = list(slot.get("legal_candidate_ids") or [])
        if "retrieval_topk_candidate_ids" in slot:
            topk = list(slot.get("retrieval_topk_candidate_ids") or [])
            legal = [candidate_id for candidate_id in topk if candidate_id in set(legal)]
        portrait_slots.append(
            {
                "slot_id": str(slot.get("slot_id") or ""),
                "required_seconds": slot.get("required_seconds"),
                "legal_candidate_ids": legal[:12],
            }
        )
    broll_slots = []
    allowed_slots: dict[str, list[str]] = {}
    all_broll_ids = [
        str(item.get("candidate_id") or "")
        for item in agent_input.get("broll_candidates") or []
        if isinstance(item, dict) and str(item.get("candidate_id") or "")
    ]
    for slot in agent_input.get("broll_slots") or []:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id") or "")
        legal = list(slot.get("retrieval_topk_candidate_ids") or all_broll_ids)
        for candidate_id in legal:
            allowed_slots.setdefault(candidate_id, []).append(slot_id)
        broll_slots.append(
            {
                "slot_id": slot_id,
                "required_seconds": slot.get("required_seconds"),
                "text": str(slot.get("text") or ""),
            }
        )
    portrait_rows = [
        [
            item.get("candidate_id"),
            item.get("asset_id"),
            item.get("available_seconds"),
            item.get("description"),
            item.get("reason"),
        ]
        for item in agent_input.get("portrait_candidates") or []
        if isinstance(item, dict)
    ]
    broll_rows = [
        [
            item.get("candidate_id"),
            item.get("asset_id"),
            item.get("diversity_key"),
            item.get("scene_name"),
            allowed_slots.get(str(item.get("candidate_id") or ""), []),
            list(item.get("matched_keywords") or [])[:6],
            item.get("available_seconds"),
            item.get("description"),
        ]
        for item in agent_input.get("broll_candidates") or []
        if isinstance(item, dict) and allowed_slots.get(str(item.get("candidate_id") or ""))
    ]
    return {
        **agent_input,
        "portrait_slots": portrait_slots,
        "broll_slots": broll_slots,
        "portrait_candidates": _prompt_candidate_lines(_PORTRAIT_CANDIDATE_HEADER, portrait_rows),
        "broll_candidates": _prompt_candidate_lines(_BROLL_CANDIDATE_HEADER, broll_rows),
    }


def _prompt_candidate_lines(header: str, rows: list[list[object]]) -> str:
    def _cell(value) -> str:
        if isinstance(value, list | tuple):
            return ",".join(_cell(item) for item in value if _cell(item))
        return " ".join(("" if value is None else str(value)).replace("|", "/").split())

    return "\n".join([header, *(" | ".join(_cell(value) for value in row) for row in rows)])


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
    if not isinstance(output.get("content"), str) or not isinstance(
        output.get("intent"), dict
    ):
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
