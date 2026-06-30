from __future__ import annotations

from fastapi import Request

from apps.api.common import (
    case_learning_repository,
    get_case,
    production_repository,
    request_id,
)
from apps.api.services.case_agent_llm import generate_script_with_llm
from packages.core import contracts as c


def script_drafts(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.ScriptDraft]:
    values = case_learning_repository(request).list_drafts(case_id=case_id, limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def adopt_script_draft(
    case_id: str, draft_id: str, payload: c.AdoptScriptDraftRequest, request: Request
) -> c.ScriptVersion:
    script = case_learning_repository(request).adopt_draft(
        case_id=case_id,
        draft_id=draft_id,
        payload=payload,
    )
    if script is None:
        raise _missing("Script draft is missing.")
    _record_adopt_reward(request, case_id, draft_id, script.id)
    return script


def _record_adopt_reward(
    request: Request, case_id: str, draft_id: str, script_version_id: str
) -> None:
    from apps.api.services import case_rubric

    case_rubric.record_adopt_reward(request, case_id, draft_id, script_version_id)


def case_performance(request: Request, case_id: str, window: str = "7d") -> c.CasePerformanceResponse:
    return production_repository(request).case_performance(case_id=case_id, window=window)


def import_metrics(case_id: str, payload: c.MetricsImportRequest, request: Request) -> c.ImportBatchReport:
    return production_repository(request).import_metrics(
        case_id=case_id,
        payload=payload,
        request_id=request_id(),
    )


def generate_script_with_memory(
    case_id: str, payload: c.GenerateScriptWithMemoryRequest, request: Request
) -> c.ScriptDraft:
    get_case(request, case_id)
    memories = _active_memory_insights(request, case_id, payload.memory_ids)
    recent_script_texts = _recent_script_texts(request, case_id)
    provider_result = generate_script_with_llm(
        case_id,
        payload.brief,
        payload.memory_ids,
        memories,
        request,
        persona_mode=payload.persona_mode,
        operation=payload.operation,
        strategy_tags=payload.strategy_tags,
        reference_script=payload.reference_script,
        duration=payload.duration,
        recent_script_texts=recent_script_texts,
    )
    draft_title = provider_result.title if provider_result and provider_result.title else _draft_title(payload)
    provider_script = provider_result.script if provider_result else None

    draft = case_learning_repository(request).generate_script_with_memory(
        case_id=case_id,
        payload=payload,
        script_override=provider_script,
        title_override=draft_title,
    )
    _score_drafts(request, case_id, draft)
    return draft


def _active_memory_insights(request: Request, case_id: str, memory_ids: list[str]) -> list[str]:
    wanted = set(memory_ids)
    if not wanted:
        return []
    memories = case_learning_repository(request).list_memory(case_id=case_id, limit=200)
    return [memory.insight for memory in memories if memory.id in wanted and memory.status == "active"]


def _recent_script_texts(request: Request, case_id: str, limit: int = 8) -> list[str]:
    return case_learning_repository(request).recent_script_texts(case_id=case_id, limit=limit)


def _draft_title(payload: c.GenerateScriptWithMemoryRequest) -> str:
    operation_labels = {
        "polish": "润色脚本",
        "fresh": "全新创作脚本",
        "remix": "参考爆款脚本",
        "clone": "爆款复刻脚本",
        "generate": "AI 生成脚本",
        "semantic": "语义提炼脚本",
    }
    scene_label = "硬广" if payload.persona_mode == "hard_ad" else "IP人设"
    return f"{scene_label} · {operation_labels.get(payload.operation, 'AI 生成脚本')}"


def _score_drafts(request: Request, case_id: str, draft: c.ScriptDraft) -> None:
    from apps.api.services import case_rubric

    case_rubric.score_drafts(request, case_id, [draft])


def _missing(message: str):
    from packages.core.workflow import NodeExecutionError

    return NodeExecutionError(c.ErrorCode.validation_invalid_options, message)
