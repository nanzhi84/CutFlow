"""EditingAgentPlanning node: one LLM综合剪辑 pass -> portrait/broll/style plans.

Replaces the three deterministic planning nodes (PortraitPlanning /
BrollPlanning / StylePlanning) with a single LLM node for the
``digital_human_editing_agent_v1`` template (issue #136). The LLM only makes
semantic ID choices; the local materializers (``_editing_agent``) turn them into
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
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._editing_agent import (
    build_agent_input,
    deterministic_selection,
    index_candidates,
    materialize_broll,
    materialize_portrait,
    materialize_style,
    portrait_asset_reuse_cap,
    portrait_cut_frames,
    select_with_repair,
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


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run

    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    boundary = state.require(ArtifactKind.plan_narration_boundary).payload or {}
    raw_units = narration.get("units", []) or []
    duration = max([float(unit.get("end", 0) or 0) for unit in raw_units] or [1.0])

    candidates = index_candidates(material)
    agent_input = build_agent_input(
        request=state.request,
        boundary=boundary,
        candidates=candidates,
        narration_units=raw_units,
        duration=duration,
    )
    reuse_cap = portrait_asset_reuse_cap(boundary=boundary, candidates=candidates)
    portrait_feasibility_failure = _portrait_feasibility_failure(agent_input)
    if portrait_feasibility_failure is not None:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "人像素材不足：portrait slot 没有可覆盖的源窗口。",
            details=portrait_feasibility_failure,
        )

    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    degradations: list[DegradationNotice] = []
    warnings: list[WarningCode] = []
    provider_invocation_ids: list[str] = []
    repair_trace: list[dict] = []

    if profile is None:
        if not sandbox_fallback_allowed():
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "未配置可用的真实 LLM 供应商（llm.chat）。请在「设置」中配置并启用真实 LLM 供应商及密钥。",
            )
        selection = deterministic_selection(
            boundary=boundary,
            candidates=candidates,
            bgm_enabled=state.request.bgm.enabled,
            max_inserts=state.request.broll.max_inserts,
        )
        mode = "deterministic_fallback_no_provider"
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
        mode = "llm"

        def _invoke(previous_errors: list[str]):
            attempt = len(provider_invocation_ids)
            prompt_invocation, rendered = ctx.prompt_registry.render(
                node_id="EditingAgentPlanning",
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
            boundary=boundary,
            candidates=candidates,
            bgm_enabled=state.request.bgm.enabled,
            max_repair_attempts=state.request.edit.max_repair_attempts,
        )
        if errors:
            raise NodeExecutionError(
                ErrorCode.prompt_output_invalid,
                f"剪辑 Agent 的选择在 {state.request.edit.max_repair_attempts} 次修复后仍不合法："
                + "；".join(errors[:5]),
            )

    # Portrait sources are scarcer than slots: the one-slot-per-asset uniqueness
    # rule was relaxed to a balanced reuse budget (both the prompt and the local
    # validator/fallback agree on it). Surface it as a graded degradation — never
    # a silent downgrade — regardless of the LLM vs deterministic path taken.
    if reuse_cap > 1:
        degradations.append(
            degradation_notice(
                WarningCode.portrait_asset_reuse_relaxed,
                f"人像可用素材数少于人像插槽数，已放松唯一性约束：同一素材最多复用 {reuse_cap} 个片段。",
                node_id=node_run.node_id,
                affects_true_yield=False,
            )
        )
        warnings.append(WarningCode.portrait_asset_reuse_relaxed)

    overlay_events = _derive_overlay_events(load_creative_intent(state).emphasis, raw_units)
    portrait_payload = materialize_portrait(
        selection=selection, boundary=boundary, candidates=candidates
    )
    broll_payload, broll_drops = materialize_broll(
        selection=selection,
        boundary=boundary,
        candidates=candidates,
        cut_frames=portrait_cut_frames(portrait_payload),
        enabled=state.request.broll.enabled,
        max_inserts=state.request.broll.max_inserts,
    )
    if broll_drops:
        selected_broll_choices = selection.broll[: max(0, state.request.broll.max_inserts)]
        affects_true_yield = bool(selected_broll_choices) and not broll_payload.get("overlays")
        degradations.append(
            degradation_notice(
                WarningCode.broll_insertions_dropped_geometry,
                f"B-roll 有 {len(broll_drops)} 个插入因时间线几何约束被丢弃。",
                node_id=node_run.node_id,
                affects_true_yield=affects_true_yield,
            ).model_copy(update={"details": {"broll_drops": broll_drops}})
        )
        warnings.append(WarningCode.broll_insertions_dropped_geometry)
    style_payload = materialize_style(
        selection=selection,
        candidates=candidates,
        request=state.request,
        overlay_events=overlay_events,
    )

    diagnostics = {
        "mode": mode,
        "instruction": state.request.edit.instruction,
        "analysis": selection.analysis,
        "repair_trace": repair_trace,
        "portrait_choices": [
            {"slot_id": c.slot_id, "window_id": c.window_id, "reason": c.reason}
            for c in selection.portrait
        ],
        "broll_choices": [
            {"slot_id": c.slot_id, "candidate_id": c.candidate_id, "reason": c.reason}
            for c in selection.broll
        ],
        "broll_drops": broll_drops,
        "font_id": selection.font_id,
        "bgm_id": selection.bgm_id,
        "portrait_asset_reuse_cap": reuse_cap,
        "candidate_counts": {
            "portrait": len(candidates.portrait_by_id),
            "broll": len(candidates.broll_by_id),
            "font": len(candidates.font_by_id),
            "bgm": len(candidates.bgm_by_id),
        },
    }

    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(ArtifactKind.plan_portrait, portrait_payload, "PortraitPlanArtifact.v1"),
            ctx.artifact(ArtifactKind.plan_broll, broll_payload, "BrollPlanArtifact.v1"),
            ctx.artifact(ArtifactKind.plan_style, style_payload, "StylePlanArtifact.v1"),
            ctx.artifact(
                ArtifactKind.plan_editing_diagnostics, diagnostics, "EditingAgentDiagnostics.v1"
            ),
        ],
        warnings=warnings,
        degradations=degradations,
        provider_invocation_ids=provider_invocation_ids,
    )
