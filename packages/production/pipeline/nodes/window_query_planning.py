"""WindowQueryPlanning: turn authoritative windows into retrieval intents."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from packages.ai.gateway import ProviderCall
from packages.core.contracts import ArtifactKind, DegradationNotice, NodeStatus, WarningCode
from packages.core.contracts.artifacts import WindowQueryPlanArtifact, WindowRetrievalQuery
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice


@dataclass(frozen=True)
class _WindowQueryInput:
    window_id: str
    kind: str
    narration_text: str


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    case_context_artifact = state.artifacts.get(ArtifactKind.case_context)
    case_context = case_context_artifact.payload if case_context_artifact is not None else {}
    creative_intent_artifact = state.artifacts.get(ArtifactKind.creative_intent)
    creative_intent = (
        creative_intent_artifact.payload if creative_intent_artifact is not None else {}
    )

    units_by_id = {
        str(unit.get("unit_id") or ""): unit
        for unit in (narration.get("units") or [])
        if isinstance(unit, dict)
    }
    window_specs = _window_specs(windows=windows, units_by_id=units_by_id)
    context_text = _context_text(
        request=state.request,
        case_context=case_context or {},
        creative_intent=creative_intent or {},
    )
    template_queries = _template_window_queries(
        window_specs=window_specs,
        context_text=context_text,
    )
    diagnostics = _base_diagnostics(windows)

    if not window_specs:
        diagnostics["source"] = "template_fallback"
        return _output(ctx, window_queries=template_queries, diagnostics=diagnostics)

    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    if profile is None:
        diagnostics["source"] = "template_fallback"
        diagnostics["fallback_reason"] = "no_provider"
        diagnostics["provider_profile_id"] = None
        return _template_fallback_output(
            ctx,
            window_queries=template_queries,
            diagnostics=diagnostics,
        )

    diagnostics["provider_profile_id"] = profile.id
    prompt_invocation, rendered = ctx.prompt_registry.render(
        node_id="WindowQueryPlanning",
        variables=_prompt_variables(
            request=state.request,
            case_context=case_context or {},
            creative_intent=creative_intent or {},
            window_specs=window_specs,
        ),
        case_id=run.case_id,
        run_id=run.id,
        node_run_id=node_run.id,
        provider_profile_id=profile.id,
    )
    provider_invocation_ids: list[str] = []
    try:
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
                idempotency_key=f"{run.id}:{node_run.id}:window_query_llm",
            )
        )
    except Exception as exc:
        diagnostics["source"] = "template_fallback"
        diagnostics["fallback_reason"] = "provider_exception"
        diagnostics["error"] = _exception_summary(exc)
        return _template_fallback_output(
            ctx,
            window_queries=template_queries,
            diagnostics=diagnostics,
        )

    provider_invocation_ids.append(invocation.id)
    if result is None or invocation.error:
        diagnostics["source"] = "template_fallback"
        diagnostics["fallback_reason"] = "provider_error"
        diagnostics["error"] = (
            invocation.error.model_dump(mode="json")
            if invocation.error
            else {"message": "WindowQueryPlanning provider returned no result."}
        )
        return _template_fallback_output(
            ctx,
            window_queries=template_queries,
            diagnostics=diagnostics,
            provider_invocation_ids=provider_invocation_ids,
        )

    payload = result.output if isinstance(result.output, dict) else {}
    nested = payload.get("intent")
    llm_payload = nested if isinstance(nested, dict) else payload
    llm_queries = _llm_queries_by_window(
        llm_payload,
        valid_window_ids={spec.window_id for spec in window_specs},
    )
    if llm_queries is None:
        diagnostics["source"] = "template_fallback"
        diagnostics["fallback_reason"] = "invalid_window_queries"
        diagnostics["error"] = {"message": "LLM output is missing usable window_queries."}
        return _template_fallback_output(
            ctx,
            window_queries=template_queries,
            diagnostics=diagnostics,
            provider_invocation_ids=provider_invocation_ids,
        )

    template_by_window = {item.window_id: item.retrieval_intent for item in template_queries}
    window_queries: list[WindowRetrievalQuery] = []
    template_backfilled_windows: list[str] = []
    for spec in window_specs:
        retrieval_intent = _trim_intent(llm_queries.get(spec.window_id, ""), limit=300)
        if not retrieval_intent:
            retrieval_intent = template_by_window.get(spec.window_id, "")
            template_backfilled_windows.append(spec.window_id)
        window_queries.append(
            WindowRetrievalQuery(
                window_id=spec.window_id,
                retrieval_intent=retrieval_intent,
            )
        )

    diagnostics["source"] = "llm_window_queries"
    if template_backfilled_windows:
        diagnostics["template_backfilled_windows"] = template_backfilled_windows
        notice = degradation_notice(
            WarningCode.window_query_template_fallback,
            f"{len(template_backfilled_windows)} 个窗口的检索 query 已用模板补齐（LLM 输出缺失）。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        )
        return _output(
            ctx,
            window_queries=window_queries,
            diagnostics=diagnostics,
            warnings=[WarningCode.window_query_template_fallback],
            degradations=[notice],
            provider_invocation_ids=provider_invocation_ids,
        )
    return _output(
        ctx,
        window_queries=window_queries,
        diagnostics=diagnostics,
        provider_invocation_ids=provider_invocation_ids,
    )


def _window_specs(*, windows: dict, units_by_id: dict[str, dict]) -> list[_WindowQueryInput]:
    specs: list[_WindowQueryInput] = []
    for window in (windows.get("portrait_windows") or []):
        if not isinstance(window, dict):
            continue
        window_id = str(window.get("window_id") or "")
        if not window_id:
            continue
        specs.append(
            _WindowQueryInput(
                window_id=window_id,
                kind="portrait",
                narration_text=_unit_text(window.get("unit_ids") or [], units_by_id),
            )
        )
    for window in (windows.get("broll_windows") or []):
        if not isinstance(window, dict):
            continue
        window_id = str(window.get("window_id") or "")
        if not window_id:
            continue
        specs.append(
            _WindowQueryInput(
                window_id=window_id,
                kind="broll",
                narration_text=str(window.get("text") or "").strip()
                or _unit_text(
                    window.get("host_unit_ids") or window.get("unit_ids") or [],
                    units_by_id,
                ),
            )
        )
    return specs


def _template_window_queries(
    *,
    window_specs: list[_WindowQueryInput],
    context_text: str,
) -> list[WindowRetrievalQuery]:
    window_queries: list[WindowRetrievalQuery] = []
    for spec in window_specs:
        if spec.kind == "portrait":
            lead = (
                "A-roll portrait talking-head source clip for this narration window. "
                "Use natural presenter delivery, stable face visibility, and "
                "lip-syncable speech."
            )
        else:
            lead = (
                "B-roll insert clip for this exact narration window. "
                "Prefer concrete visual evidence, scene detail, "
                "product/process/action, and avoid presenter talking-head footage."
            )
        window_queries.append(
            WindowRetrievalQuery(
                window_id=spec.window_id,
                retrieval_intent=_trim_intent(
                    _join_intent(
                        lead,
                        context_text,
                        f"Narration: {spec.narration_text}" if spec.narration_text else "",
                    )
                ),
            )
        )
    return window_queries


def _output(
    ctx: NodeContext,
    *,
    window_queries: list[WindowRetrievalQuery],
    diagnostics: dict[str, Any],
    warnings: list[WarningCode] | None = None,
    degradations: list[DegradationNotice] | None = None,
    provider_invocation_ids: list[str] | None = None,
) -> NodeOutput:
    payload = WindowQueryPlanArtifact(
        window_queries=window_queries,
        diagnostics=diagnostics,
    )
    notices = list(degradations or [])
    return NodeOutput(
        status=NodeStatus.degraded if notices else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_window_queries,
                payload.model_dump(mode="json"),
                "WindowQueryPlanArtifact.v1",
            )
        ],
        warnings=warnings or [],
        degradations=notices,
        provider_invocation_ids=provider_invocation_ids or [],
    )


def _unit_text(unit_ids, units_by_id: dict[str, dict]) -> str:
    parts = []
    for unit_id in unit_ids:
        unit = units_by_id.get(str(unit_id or ""))
        if unit is None:
            continue
        text = str(unit.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _template_fallback_output(
    ctx: NodeContext,
    *,
    window_queries: list[WindowRetrievalQuery],
    diagnostics: dict[str, Any],
    provider_invocation_ids: list[str] | None = None,
) -> NodeOutput:
    notice = degradation_notice(
        WarningCode.window_query_template_fallback,
        "检索 query 已回退模板拼接（LLM 不可用）。",
        node_id=ctx.node_run.node_id,
        affects_true_yield=False,
    )
    return _output(
        ctx,
        window_queries=window_queries,
        diagnostics=diagnostics,
        warnings=[WarningCode.window_query_template_fallback],
        degradations=[notice],
        provider_invocation_ids=provider_invocation_ids,
    )


def _join_intent(*parts: str) -> str:
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _prompt_variables(
    *,
    request,
    case_context: dict,
    creative_intent: dict,
    window_specs: list[_WindowQueryInput],
) -> dict[str, str]:
    return {
        "script": str(request.script or ""),
        "edit_instruction": str(getattr(request.edit, "instruction", "") or ""),
        "case_context": _case_context_text(
            case_context=case_context,
            creative_intent=creative_intent,
        ),
        "creative_beats": _creative_beats_text(creative_intent),
        "windows": json.dumps(
            [
                {
                    "window_id": spec.window_id,
                    "kind": spec.kind,
                    "narration_text": spec.narration_text,
                }
                for spec in window_specs
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def _context_text(*, request, case_context: dict, creative_intent: dict) -> str:
    case_profile = case_context.get("case_profile") if isinstance(case_context, dict) else {}
    raw_intent = creative_intent.get("intent") if isinstance(creative_intent, dict) else {}
    intent = raw_intent if isinstance(raw_intent, dict) else {}
    beats = intent.get("beats") if isinstance(intent, dict) else []
    product = str((case_profile or {}).get("product") or "").strip()
    audience = str(
        (case_profile or {}).get("target_audience") or intent.get("audience") or ""
    ).strip()
    tone = str(intent.get("tone") or "").strip() if isinstance(intent, dict) else ""
    context = " ".join(
        part
        for part in [
            f"Instruction: {request.edit.instruction}",
            f"Case product: {product}" if product else "",
            f"Audience: {audience}" if audience else "",
            f"Tone: {tone}" if tone else "",
            f"Creative beats: {'; '.join(str(beat) for beat in beats[:6])}"
            if isinstance(beats, list) and beats
            else "",
        ]
        if part
    )
    return context.strip()


def _case_context_text(*, case_context: dict, creative_intent: dict) -> str:
    case_profile = case_context.get("case_profile") if isinstance(case_context, dict) else {}
    raw_intent = creative_intent.get("intent") if isinstance(creative_intent, dict) else {}
    intent = raw_intent if isinstance(raw_intent, dict) else {}
    product = str((case_profile or {}).get("product") or "").strip()
    audience = str(
        (case_profile or {}).get("target_audience") or intent.get("audience") or ""
    ).strip()
    tone = str(intent.get("tone") or "").strip() if isinstance(intent, dict) else ""
    return _join_intent(
        f"产品：{product}" if product else "",
        f"受众：{audience}" if audience else "",
        f"语气：{tone}" if tone else "",
    )


def _creative_beats_text(creative_intent: dict) -> str:
    raw_intent = creative_intent.get("intent") if isinstance(creative_intent, dict) else {}
    intent = raw_intent if isinstance(raw_intent, dict) else {}
    beats = intent.get("beats") if isinstance(intent, dict) else []
    if not isinstance(beats, list):
        return "[]"
    return json.dumps([str(beat) for beat in beats[:12]], ensure_ascii=False)


def _base_diagnostics(windows: dict) -> dict[str, Any]:
    return {
        "source": "template_fallback",
        "portrait_window_count": len(windows.get("portrait_windows") or []),
        "broll_window_count": len(windows.get("broll_windows") or []),
    }


def _llm_queries_by_window(
    payload: dict,
    *,
    valid_window_ids: set[str],
) -> dict[str, str] | None:
    raw_queries = payload.get("window_queries") if isinstance(payload, dict) else None
    if not isinstance(raw_queries, list):
        return None
    queries: dict[str, str] = {}
    for item in raw_queries:
        if not isinstance(item, dict):
            continue
        window_id = str(item.get("window_id") or "")
        if window_id not in valid_window_ids:
            continue
        retrieval_intent = _trim_intent(str(item.get("retrieval_intent") or ""), limit=300)
        if retrieval_intent:
            queries[window_id] = retrieval_intent
    return queries or None


def _exception_summary(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _trim_intent(value: str, *, limit: int = 900) -> str:
    compact = " ".join(str(value or "").split())
    return compact[:limit]
