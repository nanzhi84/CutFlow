"""Legacy v1 HuaziPlanningSubagent compatibility implementation.

Only ``digital_human_editing_agent_v1`` calls this module. The active v2 workflow
plans captions after the main picture is rendered and must not depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from packages.ai.gateway import ProviderCall
from packages.core.contracts import DegradationNotice, ErrorCode, WarningCode
from packages.core.workflow import NodeExecutionError
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
from packages.production.pipeline._materialize import _subtitle_font_size, _subtitle_position
from packages.production.pipeline._legacy_editing_agent_planning import (
    EditingAgentContext,
    EditingAgentSelectionResult,
    _attach_provider_artifacts,
    _record_llm_request_artifact,
    _record_llm_response_artifact,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice

_HUAZI_JSON_VARS = frozenset(
    {
        "candidate_events",
        "layout_boxes",
        "track_summary",
        "animation_candidates",
        "animation_directions",
    }
)
_HUAZI_MAX_REPAIR_ATTEMPTS = 1


@dataclass(frozen=True)
class HuaziPlanningResult:
    overlay_events: list
    warnings: list[WarningCode]
    degradations: list[DegradationNotice]
    provider_invocation_ids: list[str]
    diagnostics: dict


def _empty_result(reason: str) -> HuaziPlanningResult:
    return HuaziPlanningResult(
        overlay_events=[],
        warnings=[],
        degradations=[],
        provider_invocation_ids=[],
        diagnostics={"planned": False, "reason": reason, "choices": []},
    )


def _degraded_result(
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


def _prompt_variables(agent_input: dict, previous_errors: list[str]) -> dict:
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


def _compact_box(box: dict) -> dict:
    return {
        "layout_box_id": box.get("layout_box_id"),
        "rect": box.get("rect"),
        "text_align": box.get("text_align"),
        "allowed_enter_directions": list(box.get("allowed_enter_directions") or []),
        "collision_score": box.get("collision_score"),
        "region_tags": list(box.get("region_tags") or []),
    }


def _track_summary(windows: dict) -> list[dict]:
    fps = int(windows.get("fps") or 30) or 30
    summary: list[dict] = []
    for window in windows.get("portrait_windows") or []:
        if isinstance(window, dict):
            summary.append(
                {
                    "track": "portrait",
                    "start": round(int(window.get("start_frame", 0) or 0) / fps, 2),
                    "end": round(int(window.get("end_frame", 0) or 0) / fps, 2),
                }
            )
    for window in windows.get("broll_windows") or []:
        if isinstance(window, dict):
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
    creative_intent,
) -> HuaziPlanningResult:
    """Run the historical in-node second LLM pass for v1 resume compatibility."""

    del selection_result  # retained in the legacy interface; choices are already materialized.
    state = ctx.state
    request = state.request
    run = ctx.run
    node_run = ctx.node_run
    emphasis_enabled = bool(request.subtitle.enabled and request.subtitle.emphasis_enabled)
    candidates = [
        event.model_dump(mode="json")
        for event in derive_huazi_candidates(creative_intent.emphasis, agent_context.raw_units)
    ]
    if not emphasis_enabled:
        return _empty_result("emphasis_disabled")
    if not candidates:
        return _empty_result("no_candidates")
    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    if profile is None:
        return _empty_result("no_provider")

    resolution = (int(request.output.width), int(request.output.height))
    font_size = _subtitle_font_size(request.subtitle.style_preset, request.subtitle.font_size)
    position = _subtitle_position(request.subtitle.style_preset, request.subtitle.position)
    position_y = float(position.get("y", 0.84)) if isinstance(position, dict) else 0.84
    top_y = normal_caption_top_y(
        position_y=position_y,
        font_size=font_size,
        canvas_height=resolution[1],
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
        return _empty_result("no_candidates")

    agent_input = {
        "script": request.script,
        "track_summary": _track_summary(agent_context.windows),
        "normal_caption_zone": (
            f"普通字幕大致落在画面下方（归一化 y≈{position_y}）；只有 y<{top_y} 的上方区域才是"
            "花字安全区，候选框已按此过滤，不要担心遮挡普通字幕，专注避开人脸主体即可。"
        ),
        "candidate_events": candidate_events,
        "layout_boxes": {
            event_id: [_compact_box(box) for box in boxes]
            for event_id, boxes in boxes_by_event.items()
        },
        "animation_candidates": list(HUAZI_ANIMATIONS),
        "animation_directions": HUAZI_ANIMATION_DIRECTIONS,
    }
    provider_invocation_ids: list[str] = []

    def invoke(previous_errors: list[str]):
        attempt = len(provider_invocation_ids)
        prompt_invocation, rendered = ctx.prompt_registry.render(
            node_id="HuaziPlanningSubagent",
            variables=_prompt_variables(agent_input, previous_errors),
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
                input={"prompt": rendered, "response_format": {"type": "json_object"}},
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
        payload = result.output if isinstance(result.output, dict) else {}
        nested = payload.get("intent")
        return nested if isinstance(nested, dict) else payload

    repair_trace: list[dict] = []
    errors: list[str] = []
    choices: list[HuaziPlanChoice] = []
    try:
        for attempt in range(_HUAZI_MAX_REPAIR_ATTEMPTS + 1):
            output = invoke(errors)
            choices, overreach, parse_errors = parse_huazi_plan(output)
            errors = parse_errors + validate_huazi_plan(
                choices,
                candidate_events=candidate_events,
                boxes_by_event=boxes_by_event,
                overreach_fields=overreach,
            )
            repair_trace.append({"attempt": attempt, "error_count": len(errors), "errors": errors})
            if not errors:
                break
    except NodeExecutionError as exc:
        return _degraded_result(
            node_run.node_id,
            reason="provider_error",
            repair_trace=repair_trace,
            provider_invocation_ids=provider_invocation_ids,
            detail=str(exc.error.message if exc.error else exc),
        )
    if errors:
        return _degraded_result(
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
    return HuaziPlanningResult(
        overlay_events=finalized.overlay_events,
        warnings=[],
        degradations=[],
        provider_invocation_ids=provider_invocation_ids,
        diagnostics={
            "planned": True,
            "choices": finalized.choices,
            "animation_fallbacks": finalized.animation_fallbacks,
            "density_drops": finalized.density_drops,
            "repair_attempts": max(0, len(repair_trace) - 1),
            "candidate_count": len(candidate_events),
            "repair_trace": repair_trace,
        },
    )
