"""PostProcessAgentPlanning: one whole-video BGM/caption-option selection pass."""

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
from packages.core.contracts.artifacts import (
    CaptionWindowsPlanArtifact,
    PostProcessAgentDiagnosticsArtifact,
)
from packages.core.provider_idempotency import is_provider_recovery_error
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._materialize import (
    eligible_bgm_candidates,
    materialize_style_from_selection,
)
from packages.production.pipeline._caption_window_planner import max_feasible_emphasis_count
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._postprocess_agent import (
    PostProcessSelection,
    materialize_overlay_events,
    parse_postprocess_selection,
    solve_postprocess_selection,
    unwrap_postprocess_provider_output,
    validate_postprocess_selection,
)
from packages.production.pipeline._run_state import degradation_notice

_MAX_REPAIR_ATTEMPTS = 1


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    caption_windows_artifact = state.require(ArtifactKind.plan_caption_windows)
    material_artifact = state.require(ArtifactKind.plan_material_pack)
    material = material_artifact.payload or {}
    try:
        caption_windows = CaptionWindowsPlanArtifact.model_validate(
            caption_windows_artifact.payload
        ).model_dump(mode="json")
    except Exception as exc:
        return _degraded_output(
            ctx,
            material=material,
            reason="invalid_caption_windows",
            repair_trace=[],
            candidate_counts={"bgm": 0, "caption_events": 0, "caption_options": 0},
            provider_invocation_ids=[],
            errors=[f"invalid caption windows: {exc}"],
        )
    bgm_candidates = eligible_bgm_candidates(material) if state.request.bgm.enabled else []
    emphasis_enabled = bool(
        state.request.subtitle.enabled and state.request.subtitle.emphasis_enabled
    )
    selectable_windows = (
        [
            window
            for window in (caption_windows.get("emphasis_windows") or [])
            if isinstance(window, dict) and window.get("caption_options")
        ]
        if emphasis_enabled
        else []
    )
    feasible_count = max_feasible_emphasis_count(
        selectable_windows, fps=int(caption_windows["fps"])
    )
    candidate_counts = {
        "bgm": len(bgm_candidates),
        "caption_events": len(selectable_windows),
        "caption_options": sum(
            len(window.get("caption_options") or []) for window in selectable_windows
        ),
        "caption_feasible": feasible_count,
    }

    if not bgm_candidates and not selectable_windows:
        try:
            style_payload, warnings, degradations = materialize_style_from_selection(
                request=state.request,
                material=material,
                overlay_events=[],
                bgm_id=None,
                strict_bgm_selection=True,
            )
        except Exception as exc:
            return _degraded_output(
                ctx,
                material=material,
                reason="materialization_error",
                repair_trace=[],
                candidate_counts=candidate_counts,
                provider_invocation_ids=[],
                errors=[f"style materialization failed: {exc}"],
            )
        if state.request.bgm.enabled:
            _append_bgm_unavailable(ctx, warnings, degradations)
        diagnostics = _diagnostics_payload(
            planned=True,
            reason="no_selectable_candidates",
            bgm_id=None,
            caption_choices=[],
            repair_trace=[],
            candidate_counts=candidate_counts,
            provider_invocation_ids=[],
        )
        return _output(
            ctx,
            style_payload=style_payload,
            diagnostics=diagnostics,
            warnings=warnings,
            degradations=degradations,
            provider_invocation_ids=[],
        )

    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    if profile is None:
        return _degraded_output(
            ctx,
            material=material,
            reason="no_provider",
            repair_trace=[],
            candidate_counts=candidate_counts,
            provider_invocation_ids=[],
            caption_windows=caption_windows,
            bgm_candidates=bgm_candidates,
            emphasis_enabled=emphasis_enabled,
        )

    agent_input = {
        "script": state.request.script,
        "bgm_candidates": [_compact_bgm(candidate) for candidate in bgm_candidates],
        "caption_windows": [_compact_caption_window(window) for window in selectable_windows],
        "caption_constraints": {
            "max_feasible_count": feasible_count,
            "max_selected_count": 8,
            "conflicts": list(caption_windows.get("emphasis_conflicts") or []),
            "owner": "local_solver",
        },
    }
    provider_invocation_ids: list[str] = []
    repair_trace: list[dict] = []
    errors: list[str] = []
    selection = PostProcessSelection(bgm_id=None)
    try:
        for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
            provider_output = _invoke(
                ctx=ctx,
                profile=profile,
                agent_input=agent_input,
                previous_errors=errors,
                attempt=attempt,
                provider_invocation_ids=provider_invocation_ids,
            )
            output, envelope_errors = unwrap_postprocess_provider_output(provider_output)
            selection, parse_errors = parse_postprocess_selection(output)
            errors = (
                envelope_errors
                + parse_errors
                + validate_postprocess_selection(
                    selection,
                    caption_windows=caption_windows,
                    bgm_candidates=bgm_candidates,
                    bgm_enabled=bool(state.request.bgm.enabled),
                    emphasis_enabled=emphasis_enabled,
                )
            )
            repair_trace.append(
                {"attempt": attempt, "error_count": len(errors), "errors": list(errors)}
            )
            if not errors:
                break
    except NodeExecutionError as exc:
        if is_provider_recovery_error(exc.error.code):
            raise
        return _degraded_output(
            ctx,
            material=material,
            reason="provider_error",
            repair_trace=repair_trace,
            candidate_counts=candidate_counts,
            provider_invocation_ids=provider_invocation_ids,
            provider_error=str(exc.error.message if exc.error else exc),
            caption_windows=caption_windows,
            bgm_candidates=bgm_candidates,
            selection=selection,
            emphasis_enabled=emphasis_enabled,
        )

    if errors:
        return _degraded_output(
            ctx,
            material=material,
            reason="unrepairable",
            repair_trace=repair_trace,
            candidate_counts=candidate_counts,
            provider_invocation_ids=provider_invocation_ids,
            errors=errors,
            caption_windows=caption_windows,
            bgm_candidates=bgm_candidates,
            selection=selection,
            emphasis_enabled=emphasis_enabled,
        )

    solved_selection, solver_diagnostics = solve_postprocess_selection(
        selection,
        caption_windows=caption_windows,
        bgm_candidates=bgm_candidates,
        bgm_enabled=bool(state.request.bgm.enabled),
        emphasis_enabled=emphasis_enabled,
    )
    selected_bgm_candidate = next(
        (
            candidate
            for candidate in bgm_candidates
            if candidate.get("candidate_id") == solved_selection.bgm_id
        ),
        None,
    )
    try:
        overlay_events, caption_choices = materialize_overlay_events(
            solved_selection,
            caption_windows=caption_windows,
        )
        style_payload, warnings, degradations = materialize_style_from_selection(
            request=state.request,
            material=material,
            overlay_events=overlay_events,
            bgm_id=solved_selection.bgm_id,
            strict_bgm_selection=True,
        )
    except Exception as exc:
        return _degraded_output(
            ctx,
            material=material,
            reason="materialization_error",
            repair_trace=repair_trace,
            candidate_counts=candidate_counts,
            provider_invocation_ids=provider_invocation_ids,
            errors=[f"postprocess materialization failed: {exc}"],
            caption_windows=caption_windows,
            bgm_candidates=bgm_candidates,
            selection=solved_selection,
            emphasis_enabled=emphasis_enabled,
        )
    if state.request.bgm.enabled and not bgm_candidates:
        _append_bgm_unavailable(ctx, warnings, degradations)
    diagnostics = _diagnostics_payload(
        planned=True,
        reason="selected",
        bgm_id=solved_selection.bgm_id,
        caption_choices=caption_choices,
        repair_trace=repair_trace,
        candidate_counts=candidate_counts,
        provider_invocation_ids=provider_invocation_ids,
        bgm_candidate=selected_bgm_candidate,
        solver=solver_diagnostics,
    )
    return _output(
        ctx,
        style_payload=style_payload,
        diagnostics=diagnostics,
        warnings=warnings,
        degradations=degradations,
        provider_invocation_ids=provider_invocation_ids,
    )


def _invoke(
    *,
    ctx: NodeContext,
    profile,
    agent_input: dict,
    previous_errors: list[str],
    attempt: int,
    provider_invocation_ids: list[str],
) -> object:
    variables = {
        "script": str(agent_input["script"]),
        "bgm_candidates": json.dumps(agent_input["bgm_candidates"], ensure_ascii=False),
        "caption_windows": json.dumps(agent_input["caption_windows"], ensure_ascii=False),
        "caption_constraints": json.dumps(
            agent_input.get("caption_constraints") or {}, ensure_ascii=False
        ),
        "repair_feedback": (
            "上一轮后处理选择存在以下问题，请只修正后重新输出完整 JSON：\n- "
            + "\n- ".join(previous_errors)
            if previous_errors
            else ""
        ),
    }
    prompt_invocation, rendered = ctx.prompt_registry.render(
        node_id="PostProcessAgentPlanning",
        variables=variables,
        case_id=ctx.run.case_id,
        run_id=ctx.run.id,
        node_run_id=ctx.node_run.id,
        provider_profile_id=profile.id,
    )
    request_artifact = _record_provider_request(
        ctx=ctx,
        profile=profile,
        prompt_invocation=prompt_invocation,
        rendered_prompt=rendered,
        attempt=attempt,
        previous_errors=previous_errors,
    )
    idempotency = ctx.provider_call_idempotency(
        logical_call_slot=f"postprocess_agent:attempt-{attempt}",
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
    response_artifact = _record_provider_response(
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
    provider_invocation_ids.append(invocation.id)
    if result is None or invocation.error:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "Post-process agent provider failed.",
            retryable=True,
        )
    if isinstance(result.output, dict):
        ctx.prompt_registry.validate_output(
            prompt_version_id=prompt_invocation.prompt_version_id,
            output=result.output,
        )
    return result.output


def _degraded_output(
    ctx: NodeContext,
    *,
    material: dict,
    reason: str,
    repair_trace: list[dict],
    candidate_counts: dict,
    provider_invocation_ids: list[str],
    errors: list[str] | None = None,
    provider_error: str | None = None,
    caption_windows: dict | None = None,
    bgm_candidates: list[dict] | None = None,
    selection: PostProcessSelection | None = None,
    emphasis_enabled: bool | None = None,
) -> NodeOutput:
    bgm_candidates = (
        list(bgm_candidates)
        if bgm_candidates is not None
        else (eligible_bgm_candidates(material) if ctx.state.request.bgm.enabled else [])
    )
    solved_selection = PostProcessSelection(bgm_id=None)
    solver_diagnostics: dict = {}
    if caption_windows is not None:
        solved_selection, solver_diagnostics = solve_postprocess_selection(
            selection or PostProcessSelection(bgm_id=None),
            caption_windows=caption_windows,
            bgm_candidates=bgm_candidates,
            bgm_enabled=bool(ctx.state.request.bgm.enabled),
            emphasis_enabled=(
                bool(emphasis_enabled)
                if emphasis_enabled is not None
                else bool(
                    ctx.state.request.subtitle.enabled
                    and ctx.state.request.subtitle.emphasis_enabled
                )
            ),
            deterministic_fallback=True,
        )
    overlay_events = []
    caption_choices: list[dict] = []
    if caption_windows is not None:
        overlay_events, caption_choices = materialize_overlay_events(
            solved_selection,
            caption_windows=caption_windows,
        )
    try:
        style_payload, warnings, degradations = materialize_style_from_selection(
            request=ctx.state.request,
            material=material,
            overlay_events=overlay_events,
            bgm_id=solved_selection.bgm_id,
            strict_bgm_selection=True,
        )
    except Exception as exc:
        if errors is None:
            errors = []
        errors.append(f"fallback materialization failed: {exc}")
        style_payload, warnings, degradations = materialize_style_from_selection(
            request=ctx.state.request,
            material={},
            overlay_events=[],
            bgm_id=None,
            strict_bgm_selection=True,
        )
        solved_selection = PostProcessSelection(bgm_id=None)
        caption_choices = []
        solver_diagnostics = {
            **solver_diagnostics,
            "selected_count": 0,
            "used_deterministic_fallback": True,
        }
    if ctx.state.request.bgm.enabled and not bgm_candidates:
        _append_bgm_unavailable(ctx, warnings, degradations)
    details: dict = {"reason": reason, "repair_trace": repair_trace}
    if errors:
        details["errors"] = errors[:5]
    if provider_error:
        details["provider_error"] = provider_error
    details["preserved_caption_event_ids"] = [choice["event_id"] for choice in caption_choices]
    details["preserved_bgm_id"] = solved_selection.bgm_id
    warnings.append(WarningCode.postprocess_planning_failed)
    degradations.append(
        degradation_notice(
            WarningCode.postprocess_planning_failed,
            "后处理 Agent 未完整成功；普通字幕保留，并已独立保留可由本地规则证明合法的花字/BGM选择。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        ).model_copy(update={"details": details})
    )
    selected_bgm_candidate = next(
        (
            candidate
            for candidate in bgm_candidates
            if candidate.get("candidate_id") == solved_selection.bgm_id
        ),
        None,
    )
    diagnostics = _diagnostics_payload(
        planned=False,
        reason=reason,
        bgm_id=solved_selection.bgm_id,
        caption_choices=caption_choices,
        repair_trace=repair_trace,
        candidate_counts=candidate_counts,
        provider_invocation_ids=provider_invocation_ids,
        bgm_candidate=selected_bgm_candidate,
        solver=solver_diagnostics,
    )
    return _output(
        ctx,
        style_payload=style_payload,
        diagnostics=diagnostics,
        warnings=warnings,
        degradations=degradations,
        provider_invocation_ids=provider_invocation_ids,
    )


def _output(
    ctx: NodeContext,
    *,
    style_payload: dict,
    diagnostics: dict,
    warnings: list[WarningCode],
    degradations: list[DegradationNotice],
    provider_invocation_ids: list[str],
) -> NodeOutput:
    diagnostics = PostProcessAgentDiagnosticsArtifact.model_validate(diagnostics).model_dump(
        mode="json"
    )
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(ArtifactKind.plan_style, style_payload, "StylePlanArtifact.v1"),
            ctx.artifact(
                ArtifactKind.plan_postprocess_diagnostics,
                diagnostics,
                "PostProcessAgentDiagnostics.v1",
            ),
        ],
        warnings=warnings,
        degradations=degradations,
        provider_invocation_ids=provider_invocation_ids,
    )


def _diagnostics_payload(
    *,
    planned: bool,
    reason: str,
    bgm_id: str | None,
    caption_choices: list[dict],
    repair_trace: list[dict],
    candidate_counts: dict,
    provider_invocation_ids: list[str],
    bgm_candidate: dict | None = None,
    solver: dict | None = None,
) -> dict:
    metadata = (
        bgm_candidate.get("metadata")
        if isinstance(bgm_candidate, dict) and isinstance(bgm_candidate.get("metadata"), dict)
        else {}
    )
    return {
        "policy_version": "postprocess_agent_v1",
        "planned": planned,
        "reason": reason,
        "bgm_id": bgm_id,
        "candidate_id": bgm_candidate.get("candidate_id") if bgm_candidate else None,
        "asset_id": bgm_candidate.get("asset_id") if bgm_candidate else None,
        "segment_id": metadata.get("clip_id") if bgm_candidate else None,
        "caption_choices": caption_choices,
        "repair_trace": repair_trace,
        "candidate_counts": candidate_counts,
        "solver": solver or {},
        "provider_invocation_ids": provider_invocation_ids,
    }


def _compact_bgm(candidate: dict) -> dict:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    return {
        "bgm_id": str(candidate.get("candidate_id") or ""),
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "asset_id": str(candidate.get("asset_id") or ""),
        "segment_id": str(metadata.get("clip_id") or ""),
        "mood": str(metadata.get("mood") or ""),
        "energy_profile": str(metadata.get("energy_profile") or ""),
        "scene_fit": list(metadata.get("scene_fit") or []),
        "script_fit": list(metadata.get("script_fit") or []),
        "avoid_script": list(metadata.get("avoid_script") or []),
        "reason": str(candidate.get("reason") or metadata.get("reason") or ""),
    }


def _compact_caption_window(window: dict) -> dict:
    anchors = {
        str(anchor.get("anchor_id") or ""): anchor
        for anchor in (window.get("anchor_candidates") or [])
        if isinstance(anchor, dict)
    }
    options = []
    for option in window.get("caption_options") or []:
        if not isinstance(option, dict):
            continue
        preset_id = str(option.get("visual_preset_id") or "emphasis")
        anchor = anchors.get(str(option.get("anchor_id") or ""), {})
        region = "/".join(str(item) for item in (anchor.get("region_tags") or [])[:2])
        label = "切镜巨字" if preset_id == "hero" else "重点黄白字"
        options.append(
            {
                "caption_option_id": option.get("caption_option_id"),
                "label": f"{label} · {region or '安全区域'}",
            }
        )
    return {
        "event_id": window.get("event_id"),
        "text": window.get("text"),
        "caption_options": options,
    }


def _append_bgm_unavailable(
    ctx: NodeContext,
    warnings: list[WarningCode],
    degradations: list[DegradationNotice],
) -> None:
    warnings.append(WarningCode.bgm_skipped_library_unannotated)
    degradations.append(
        degradation_notice(
            WarningCode.bgm_skipped_library_unannotated,
            "BGM 库没有可用的已标注片段，本次不混入 BGM。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        )
    )


def _record_provider_request(
    *,
    ctx: NodeContext,
    profile,
    prompt_invocation,
    rendered_prompt: str,
    attempt: int,
    previous_errors: list[str],
) -> Artifact:
    return ctx.artifact(
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
            "prompt": rendered_prompt,
        },
        "PostProcessAgentLlmRequestSnapshot.v1",
    )


def _record_provider_response(*, ctx: NodeContext, invocation, result, attempt: int) -> Artifact:
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
            "status": (
                invocation.status.value
                if hasattr(invocation.status, "value")
                else str(invocation.status)
            ),
            "error": (
                invocation.error.model_dump(mode="json")
                if getattr(invocation, "error", None)
                else None
            ),
            "output": result.output if result is not None else None,
        },
        "PostProcessAgentLlmResponseSnapshot.v1",
    )


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
