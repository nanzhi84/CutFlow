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
    ArtifactKind,
    DegradationNotice,
    ErrorCode,
    NodeStatus,
    WarningCode,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._editing_agent import (
    build_agent_input,
    deterministic_selection,
    index_candidates,
    materialize_broll,
    materialize_portrait,
    materialize_style,
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

    overlay_events = _derive_overlay_events(load_creative_intent(state).emphasis, raw_units)
    portrait_payload = materialize_portrait(
        selection=selection, boundary=boundary, candidates=candidates
    )
    broll_payload = materialize_broll(
        selection=selection,
        boundary=boundary,
        candidates=candidates,
        cut_frames=portrait_cut_frames(portrait_payload),
        enabled=state.request.broll.enabled,
        max_inserts=state.request.broll.max_inserts,
    )
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
        "font_id": selection.font_id,
        "bgm_id": selection.bgm_id,
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
