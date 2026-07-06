"""WindowQueryPlanning: turn authoritative windows into retrieval intents."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind
from packages.core.contracts.artifacts import WindowQueryPlanArtifact, WindowRetrievalQuery
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    case_context_artifact = state.artifacts.get(ArtifactKind.case_context)
    case_context = case_context_artifact.payload if case_context_artifact is not None else {}
    creative_intent_artifact = state.artifacts.get(ArtifactKind.creative_intent)
    creative_intent = creative_intent_artifact.payload if creative_intent_artifact is not None else {}

    units_by_id = {
        str(unit.get("unit_id") or ""): unit
        for unit in (narration.get("units") or [])
        if isinstance(unit, dict)
    }
    context_text = _context_text(
        request=state.request,
        case_context=case_context or {},
        creative_intent=creative_intent or {},
    )
    window_queries: list[WindowRetrievalQuery] = []
    for window in (windows.get("portrait_windows") or []):
        if not isinstance(window, dict):
            continue
        window_id = str(window.get("window_id") or "")
        if not window_id:
            continue
        unit_text = _unit_text(window.get("unit_ids") or [], units_by_id)
        window_queries.append(
            WindowRetrievalQuery(
                window_id=window_id,
                retrieval_intent=_trim_intent(
                    _join_intent(
                        "A-roll portrait talking-head source clip for this narration window. "
                        "Use natural presenter delivery, stable face visibility, and "
                        "lip-syncable speech.",
                        context_text,
                        f"Narration: {unit_text}" if unit_text else "",
                    )
                ),
            )
        )
    for window in (windows.get("broll_windows") or []):
        if not isinstance(window, dict):
            continue
        window_id = str(window.get("window_id") or "")
        if not window_id:
            continue
        unit_text = str(window.get("text") or "").strip() or _unit_text(
            window.get("host_unit_ids") or window.get("unit_ids") or [],
            units_by_id,
        )
        window_queries.append(
            WindowRetrievalQuery(
                window_id=window_id,
                retrieval_intent=_trim_intent(
                    _join_intent(
                        "B-roll insert clip for this exact narration window. "
                        "Prefer concrete visual evidence, scene detail, "
                        "product/process/action, and avoid presenter talking-head footage.",
                        context_text,
                        f"Narration: {unit_text}" if unit_text else "",
                    )
                ),
            )
        )

    payload = WindowQueryPlanArtifact(
        window_queries=window_queries,
        diagnostics={
            "source": "authoritative_timeline_windows",
            "portrait_window_count": len(windows.get("portrait_windows") or []),
            "broll_window_count": len(windows.get("broll_windows") or []),
        },
    )
    return NodeOutput(
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_window_queries,
                payload.model_dump(mode="json"),
                "WindowQueryPlanArtifact.v1",
            )
        ]
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


def _join_intent(*parts: str) -> str:
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _context_text(*, request, case_context: dict, creative_intent: dict) -> str:
    case_profile = case_context.get("case_profile") if isinstance(case_context, dict) else {}
    raw_intent = creative_intent.get("intent") if isinstance(creative_intent, dict) else {}
    intent = raw_intent if isinstance(raw_intent, dict) else {}
    beats = intent.get("beats") if isinstance(intent, dict) else []
    product = str((case_profile or {}).get("product") or "").strip()
    audience = str((case_profile or {}).get("target_audience") or intent.get("audience") or "").strip()
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


def _trim_intent(value: str, *, limit: int = 900) -> str:
    compact = " ".join(str(value or "").split())
    return compact[:limit]
