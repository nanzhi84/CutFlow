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
    EditingSelection,
    IndexedCandidates,
    build_agent_input,
    deterministic_selection,
    index_candidates,
    select_with_repair,
)
from packages.production.pipeline._materialize import (
    materialize_broll_from_assignment,
    materialize_portrait_from_assignment,
    materialize_style_from_selection,
    portrait_cut_frames,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline.nodes._creative_intent import load_creative_intent
from packages.production.pipeline.nodes.style_planning import _derive_overlay_events

# Structured variables serialized as JSON for the prompt; scalars go through str().
_JSON_VARS = frozenset(
    {
        "narration_units",
        "safe_cut_boundaries",
        "portrait_slots",
        "broll_slots",
        "portrait_candidates",
        "broll_candidates",
        "font_candidates",
        "bgm_candidates",
    }
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
        if not slot.get("legal_window_ids") and str(slot.get("slot_id") or "")
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
    return {
        "slot_id": str(window.get("window_id") or ""),
        "start_frame": start_frame,
        "end_frame": end_frame,
        "length_frames": int(window.get("length_frames") or max(0, end_frame - start_frame)),
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
) -> EditingAgentContext:
    raw_units = narration.get("units", []) or []
    duration = max([float(unit.get("end", 0) or 0) for unit in raw_units] or [1.0])

    agent_boundary = _boundary_with_compiled_windows(boundary, windows)
    shortlisted_material, shortlist_counts = shortlist_for_windows(
        windows.get("portrait_windows", []) or [],
        windows.get("broll_windows", []) or [],
        material,
    )
    candidates = index_candidates(shortlisted_material)
    agent_input = build_agent_input(
        request=request,
        boundary=agent_boundary,
        candidates=candidates,
        narration_units=raw_units,
        duration=duration,
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
        agent_input=agent_input,
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

    if profile is None:
        if not sandbox_fallback_allowed():
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "未配置可用的真实 LLM 供应商（llm.chat）。"
                "请在「设置」中配置并启用真实 LLM 供应商及密钥。",
            )
        selection = deterministic_selection(
            boundary=agent_context.agent_boundary,
            candidates=agent_context.candidates,
            bgm_enabled=state.request.bgm.enabled,
            max_inserts=state.request.broll.max_inserts,
        )
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

        def _invoke(previous_errors: list[str]):
            attempt = len(provider_invocation_ids)
            prompt_invocation, rendered = ctx.prompt_registry.render(
                node_id="EditingAgentPlanning",
                variables=_prompt_variables(agent_context.agent_input, previous_errors),
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
                    input={"prompt": rendered},
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
        )
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
                max_inserts=state.request.broll.max_inserts,
            )
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
    creative_intent,
) -> EditingAgentMaterializedOutputs:
    selection = selection_result.selection
    warnings = list(selection_result.warnings)
    degradations = list(selection_result.degradations)
    windows = agent_context.windows
    candidates = agent_context.candidates
    fallback_used = selection_result.fallback_used

    portrait_assignment = (
        _default_portrait_assignment(windows)
        if fallback_used
        else _selection_portrait_assignment(selection)
    )
    broll_assignment = _selection_broll_assignment(selection)
    assignment_for_materialize = {
        "portrait": portrait_assignment,
        "broll": broll_assignment,
    }
    portrait_payload = (
        _default_portrait_payload(windows)
        if fallback_used
        else materialize_portrait_from_assignment(
            windows=windows,
            assignment=assignment_for_materialize,
            candidates=candidates,
        )
    )
    broll_payload, broll_drops = materialize_broll_from_assignment(
        windows=windows,
        assignment=assignment_for_materialize,
        candidates=candidates,
        cut_frames=portrait_cut_frames(portrait_payload),
        enabled=request.broll.enabled,
        max_inserts=request.broll.max_inserts,
    )
    if broll_drops:
        selected_broll_choices = selection.broll[: max(0, request.broll.max_inserts)]
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
    overlay_events = _derive_overlay_events(creative_intent.emphasis, agent_context.raw_units)
    style_payload, style_warnings, style_degradations = materialize_style_from_selection(
        request=request,
        material=agent_context.shortlisted_material,
        overlay_events=overlay_events,
        font_id=selection.font_id,
        bgm_id=selection.bgm_id,
    )
    warnings.extend(style_warnings)
    degradations.extend(
        notice.model_copy(update={"node_id": node_id}) for notice in style_degradations
    )

    assignment_diagnostics = {
        "repair_trace": selection_result.repair_trace,
        "shortlist_counts": agent_context.shortlist_counts,
        "fallback_used": selection_result.fallback_used,
        "fallback_reason": selection_result.fallback_reason,
        "broll_drops": broll_drops,
    }
    assignment_payload = _assignment_payload(
        engine=selection_result.engine,
        portrait=portrait_assignment,
        broll=broll_assignment,
        font_id=selection.font_id,
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
        "font_id": selection.font_id,
        "bgm_id": selection.bgm_id,
        "shortlist_counts": agent_context.shortlist_counts,
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


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    boundary = state.require(ArtifactKind.plan_narration_boundary).payload or {}
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}

    agent_context = build_editing_agent_context(
        request=state.request,
        material=material,
        narration=narration,
        boundary=boundary,
        windows=windows,
    )
    selection_result = select_editing_assignment(ctx=ctx, agent_context=agent_context)
    materialized = materialize_editing_outputs(
        request=state.request,
        node_id=ctx.node_run.node_id,
        agent_context=agent_context,
        selection_result=selection_result,
        creative_intent=load_creative_intent(state),
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
        provider_invocation_ids=selection_result.provider_invocation_ids,
    )
