"""BgmAgentPlanning: one LLM selection over locally legal BGM segment IDs."""

from __future__ import annotations

import json

from packages.ai.gateway import ProviderCall
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DegradationNotice,
    ErrorCode,
    NodeStatus,
    WarningCode,
    utcnow,
)
from packages.core.contracts.artifacts import BgmAgentDiagnosticsArtifact
from packages.core.provider_idempotency import is_provider_recovery_error
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._materialize import (
    eligible_bgm_candidates,
    materialize_style_from_selection,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice

_MAX_REPAIR_ATTEMPTS = 1


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    candidates = eligible_bgm_candidates(material) if state.request.bgm.enabled else []
    if not candidates:
        style, warnings, degradations = materialize_style_from_selection(
            request=state.request,
            material=material,
            bgm_id=None,
            strict_bgm_selection=True,
        )
        if state.request.bgm.enabled:
            warnings.append(WarningCode.bgm_skipped_library_unannotated)
            degradations.append(
                degradation_notice(
                    WarningCode.bgm_skipped_library_unannotated,
                    "BGM 库没有可用的已标注片段，本次不混入 BGM。",
                    node_id=ctx.node_run.node_id,
                    affects_true_yield=False,
                )
            )
        return _output(
            ctx,
            style=style,
            diagnostics=_diagnostics(
                planned=True,
                reason="no_candidates",
                bgm_id=None,
                analysis="",
                repair_trace=[],
                candidates=candidates,
                provider_invocation_ids=[],
            ),
            warnings=warnings,
            degradations=degradations,
            provider_invocation_ids=[],
        )

    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    if profile is None:
        return _fallback(ctx, material=material, candidates=candidates, reason="no_provider")

    provider_invocation_ids: list[str] = []
    repair_trace: list[dict] = []
    errors: list[str] = []
    bgm_id: str | None = None
    analysis = ""
    try:
        for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
            raw = _invoke(
                ctx=ctx,
                profile=profile,
                candidates=candidates,
                previous_errors=errors,
                attempt=attempt,
                provider_invocation_ids=provider_invocation_ids,
            )
            output, errors = _unwrap(raw)
            selected, parse_errors = _parse(output, candidates=candidates)
            errors.extend(parse_errors)
            bgm_id = selected
            analysis = str(output.get("analysis") or "")
            repair_trace.append(
                {"attempt": attempt, "error_count": len(errors), "errors": list(errors)}
            )
            if not errors:
                break
    except NodeExecutionError as exc:
        if is_provider_recovery_error(exc.error.code):
            raise
        return _fallback(
            ctx,
            material=material,
            candidates=candidates,
            reason="provider_error",
            repair_trace=repair_trace,
            provider_invocation_ids=provider_invocation_ids,
            errors=[str(exc.error.message)],
        )
    if errors:
        return _fallback(
            ctx,
            material=material,
            candidates=candidates,
            reason="unrepairable",
            repair_trace=repair_trace,
            provider_invocation_ids=provider_invocation_ids,
            errors=errors,
        )
    style, warnings, degradations = materialize_style_from_selection(
        request=state.request,
        material=material,
        bgm_id=bgm_id,
        strict_bgm_selection=True,
    )
    return _output(
        ctx,
        style=style,
        diagnostics=_diagnostics(
            planned=True,
            reason="selected",
            bgm_id=bgm_id,
            analysis=analysis,
            repair_trace=repair_trace,
            candidates=candidates,
            provider_invocation_ids=provider_invocation_ids,
        ),
        warnings=warnings,
        degradations=degradations,
        provider_invocation_ids=provider_invocation_ids,
    )


def _parse(output: dict, *, candidates: list[dict]) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    if set(output) != {"bgm_id", "analysis"}:
        errors.append("BGM response must contain exactly bgm_id and analysis")
    if not isinstance(output.get("analysis"), str):
        errors.append("BGM response analysis must be a string")
    value = output.get("bgm_id")
    if value is not None and not isinstance(value, str):
        errors.append("BGM response bgm_id must be null or a string")
        return None, errors
    bgm_id = value.strip() if isinstance(value, str) else None
    known = {str(item.get("candidate_id") or "") for item in candidates}
    if bgm_id is not None and bgm_id not in known:
        errors.append(f"bgm_id '{bgm_id}' is not a known candidate")
    return bgm_id, errors


def _unwrap(output: object) -> tuple[dict, list[str]]:
    if not isinstance(output, dict):
        return {}, ["BGM provider output must be a JSON object"]
    if "content" not in output and "intent" not in output:
        return output, []
    errors: list[str] = []
    if set(output) != {"content", "intent"}:
        errors.append("BGM provider envelope must contain exactly content and intent")
    if not isinstance(output.get("content"), str):
        errors.append("BGM provider envelope content must be a string")
    intent = output.get("intent")
    if not isinstance(intent, dict):
        errors.append("BGM provider envelope intent must be an object")
        return {}, errors
    return intent, errors


def _invoke(
    *,
    ctx: NodeContext,
    profile,
    candidates: list[dict],
    previous_errors: list[str],
    attempt: int,
    provider_invocation_ids: list[str],
) -> object:
    variables = {
        "script": ctx.state.request.script,
        "bgm_candidates": json.dumps([_compact(item) for item in candidates], ensure_ascii=False),
        "repair_feedback": (
            "上一轮 BGM 选择存在以下问题，请只修正后输出完整 JSON：\n- "
            + "\n- ".join(previous_errors)
            if previous_errors
            else ""
        ),
    }
    prompt_invocation, rendered = ctx.prompt_registry.render(
        node_id="BgmAgentPlanning",
        variables=variables,
        case_id=ctx.run.case_id,
        run_id=ctx.run.id,
        node_run_id=ctx.node_run.id,
        provider_profile_id=profile.id,
    )
    request_artifact = ctx.artifact(
        ArtifactKind.provider_raw_request,
        {
            "capability_id": "llm.chat",
            "provider_profile_id": profile.id,
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "prompt_version_id": prompt_invocation.prompt_version_id,
            "prompt_invocation_id": prompt_invocation.id,
            "attempt": attempt,
            "previous_errors": list(previous_errors),
            "prompt": rendered,
        },
        "BgmAgentLlmRequestSnapshot.v1",
    )
    idempotency = ctx.provider_call_idempotency(
        logical_call_slot=f"bgm_agent:attempt-{attempt}",
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
    response_artifact = ctx.artifact(
        ArtifactKind.provider_raw_response,
        {
            "capability_id": "llm.chat",
            "provider_invocation_id": invocation.id,
            "provider_profile_id": invocation.provider_profile_id,
            "provider_id": invocation.provider_id,
            "model_id": invocation.model_id,
            "prompt_version_id": invocation.prompt_version_id,
            "attempt": attempt,
            "status": invocation.status.value,
            "error": invocation.error.model_dump(mode="json") if invocation.error else None,
            "output": result.output if result is not None else None,
        },
        "BgmAgentLlmResponseSnapshot.v1",
    )
    _attach_provider_artifacts(
        ctx=ctx,
        invocation_id=invocation.id,
        request_artifact=request_artifact,
        response_artifact=response_artifact,
    )
    provider_invocation_ids.append(invocation.id)
    if result is None or invocation.error:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "BGM agent provider failed.",
            retryable=True,
        )
    if isinstance(result.output, dict):
        ctx.prompt_registry.validate_output(
            prompt_version_id=prompt_invocation.prompt_version_id,
            output=result.output,
        )
    return result.output


def _fallback(
    ctx: NodeContext,
    *,
    material: dict,
    candidates: list[dict],
    reason: str,
    repair_trace: list[dict] | None = None,
    provider_invocation_ids: list[str] | None = None,
    errors: list[str] | None = None,
) -> NodeOutput:
    repair_trace = list(repair_trace or [])
    provider_invocation_ids = list(provider_invocation_ids or [])
    style, warnings, degradations = materialize_style_from_selection(
        request=ctx.state.request,
        material=material,
        strict_bgm_selection=False,
    )
    selected_asset_id = (
        (style.get("bgm") or {}).get("asset_id")
        if isinstance(style.get("bgm"), dict)
        else None
    )
    selected_segment_id = (
        (style.get("bgm") or {}).get("segment_id")
        if isinstance(style.get("bgm"), dict)
        else None
    )
    bgm_id = next(
        (
            str(item.get("candidate_id"))
            for item in candidates
            if item.get("asset_id") == selected_asset_id
            and (item.get("metadata") or {}).get("clip_id") == selected_segment_id
        ),
        None,
    )
    warnings.append(WarningCode.bgm_planning_failed)
    degradations.append(
        degradation_notice(
            WarningCode.bgm_planning_failed,
            "BGM Agent 未成功，已使用确定性本地选择。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        ).model_copy(update={"details": {"reason": reason, "errors": list(errors or [])[:5]}})
    )
    return _output(
        ctx,
        style=style,
        diagnostics=_diagnostics(
            planned=False,
            reason=reason,
            bgm_id=bgm_id,
            analysis="",
            repair_trace=repair_trace,
            candidates=candidates,
            provider_invocation_ids=provider_invocation_ids,
        ),
        warnings=warnings,
        degradations=degradations,
        provider_invocation_ids=provider_invocation_ids,
    )


def _output(
    ctx: NodeContext,
    *,
    style: dict,
    diagnostics: dict,
    warnings: list[WarningCode],
    degradations: list[DegradationNotice],
    provider_invocation_ids: list[str],
) -> NodeOutput:
    payload = BgmAgentDiagnosticsArtifact.model_validate(diagnostics).model_dump(mode="json")
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(ArtifactKind.plan_style, style, "StylePlanArtifact.v1"),
            ctx.artifact(
                ArtifactKind.plan_bgm_diagnostics,
                payload,
                "BgmAgentDiagnostics.v1",
            ),
        ],
        warnings=warnings,
        degradations=degradations,
        provider_invocation_ids=provider_invocation_ids,
    )


def _diagnostics(
    *,
    planned: bool,
    reason: str,
    bgm_id: str | None,
    analysis: str,
    repair_trace: list[dict],
    candidates: list[dict],
    provider_invocation_ids: list[str],
) -> dict:
    candidate = next(
        (item for item in candidates if item.get("candidate_id") == bgm_id),
        None,
    )
    metadata = candidate["metadata"] if candidate else {}
    return {
        "policy_version": "bgm_agent_v1",
        "planned": planned,
        "reason": reason,
        "bgm_id": bgm_id,
        "asset_id": candidate.get("asset_id") if candidate else None,
        "segment_id": metadata.get("clip_id") if candidate else None,
        "analysis": analysis,
        "repair_trace": repair_trace,
        "candidate_count": len(candidates),
        "provider_invocation_ids": provider_invocation_ids,
    }


def _compact(candidate: dict) -> dict:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    return {
        "bgm_id": str(candidate.get("candidate_id") or ""),
        "asset_id": str(candidate.get("asset_id") or ""),
        "segment_id": str(metadata.get("clip_id") or ""),
        "mood": str(metadata.get("mood") or ""),
        "energy_profile": str(metadata.get("energy_profile") or ""),
        "scene_fit": list(metadata.get("scene_fit") or []),
        "script_fit": list(metadata.get("script_fit") or []),
        "avoid_script": list(metadata.get("avoid_script") or []),
    }


def _attach_provider_artifacts(
    *,
    ctx: NodeContext,
    invocation_id: str,
    request_artifact: Artifact,
    response_artifact: Artifact,
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
