from __future__ import annotations

from fastapi import Request

from apps.api.common import (
    case_learning_repository,
    get_case,
    page,
    production_repository,
    repository,
    request_id,
)
from apps.api.services.case_agent_llm import generate_script_with_llm
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.creative.cases import evolution, metrics_import


def script_drafts(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.ScriptDraft]:
    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_drafts(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).drafts.values() if item.case_id == case_id], limit)


def adopt_script_draft(
    case_id: str, draft_id: str, payload: c.AdoptScriptDraftRequest, request: Request
) -> c.ScriptVersion:
    if case_learning_repository(request) is not None:
        script = case_learning_repository(request).adopt_draft(
            case_id=case_id,
            draft_id=draft_id,
            payload=payload,
        )
        if script is None:
            raise _missing("Script draft is missing.")
        _record_adopt_reward(request, case_id, draft_id, script.id)
        return script

    draft = repository(request).drafts[draft_id]
    script = c.ScriptVersion(
        id=new_id("script"),
        case_id=case_id,
        title=payload.title or draft.title,
        script=payload.publish_content or draft.script,
        adopted_from_draft_id=draft.id,
    )
    repository(request).scripts[script.id] = script
    repository(request).drafts[draft.id] = draft.model_copy(
        update={"status": "adopted", "updated_at": c.utcnow()}
    )
    _record_adopt_reward(request, case_id, draft_id, script.id)
    return script


def _record_adopt_reward(
    request: Request, case_id: str, draft_id: str, script_version_id: str
) -> None:
    from apps.api.services import case_rubric

    case_rubric.record_adopt_reward(request, case_id, draft_id, script_version_id)


def case_performance(request: Request, case_id: str, window: str = "7d") -> c.CasePerformanceResponse:
    if production_repository(request) is not None:
        return production_repository(request).case_performance(case_id=case_id, window=window)

    observations = [item for item in repository(request).performance_observations.values() if item.case_id == case_id]
    metrics = c.PerformanceMetricView(
        impressions=int(sum(item.metric_value for item in observations if item.metric_name == "impressions")),
        views=int(sum(item.metric_value for item in observations if item.metric_name == "views")),
        likes=int(sum(item.metric_value for item in observations if item.metric_name == "likes")),
    )
    obs_ids = {obs.id for obs in observations}
    scores = [
        score
        for score in repository(request).performance_scores.values()
        if score.case_id == case_id and score.observation_id in obs_ids
    ]
    return c.CasePerformanceResponse(metrics=metrics, observations=observations, scores=scores)


def import_metrics(case_id: str, payload: c.MetricsImportRequest, request: Request) -> c.ImportBatchReport:
    if production_repository(request) is not None:
        return production_repository(request).import_metrics(
            case_id=case_id,
            payload=payload,
            request_id=request_id(),
        )

    repo = repository(request)
    records = [
        metrics_import.PublishRecordIndex(
            publish_record_id=record.id,
            video_version_id=record.video_version_id,
            platform=record.platform,
        )
        for record in repo.publish_records.values()
        if record.case_id == case_id
    ]
    result = metrics_import.match_metrics_rows(
        payload.rows,
        policy=payload.matching_policy,
        records=records,
        default_platform=payload.platform,
        default_account_id=payload.account_id,
    )
    results: list[c.ImportRowResult] = []
    for matched in result.matched:
        obs = metrics_import.observation_contract_from_match(case_id, matched)
        if not payload.dry_run:
            repo.performance_observations[obs.id] = obs
            score = evolution.compute_performance_score(obs)
            repo.performance_scores[score.id] = score
        results.append(c.ImportRowResult(row_index=matched.row_index, status="created", internal_id=obs.id))
    for unmatched in result.unmatched:
        results.append(
            c.ImportRowResult(
                row_index=unmatched.row_index,
                status="skipped",
                error=c.NodeError(code=c.ErrorCode.validation_invalid_options, message=unmatched.reason),
            )
        )
    results.sort(key=lambda item: item.row_index)
    report = c.ImportBatchReport(
        batch_id=new_id("imp"),
        import_type="performance",
        status=c.ImportBatchStatus.completed
        if not result.unmatched
        else c.ImportBatchStatus.partially_failed,
        created_count=len(result.matched),
        skipped_count=len(result.unmatched),
        failed_count=0,
        results=results,
        request_id=request_id(),
    )
    repo.import_reports[report.batch_id] = report
    return report


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

    if case_learning_repository(request) is not None:
        draft = case_learning_repository(request).generate_script_with_memory(
            case_id=case_id,
            payload=payload,
            script_override=provider_script,
            title_override=draft_title,
        )
    else:
        draft = c.ScriptDraft(
            id=new_id("draft"),
            case_id=case_id,
            title=draft_title,
            script=provider_script or f"{payload.brief}\n\n参考记忆：{' / '.join(memories) if memories else '暂无'}",
            memory_ids=payload.memory_ids,
        )
        repository(request).drafts[draft.id] = draft

    _score_drafts(request, case_id, draft)
    return draft


def _active_memory_insights(request: Request, case_id: str, memory_ids: list[str]) -> list[str]:
    wanted = set(memory_ids)
    if not wanted:
        return []
    if case_learning_repository(request) is not None:
        memories = case_learning_repository(request).list_memory(case_id=case_id, limit=200)
    else:
        memories = [item for item in repository(request).memories.values() if item.case_id == case_id]
    return [memory.insight for memory in memories if memory.id in wanted and memory.status == "active"]


def _recent_script_texts(request: Request, case_id: str, limit: int = 8) -> list[str]:
    if case_learning_repository(request) is not None:
        return case_learning_repository(request).recent_script_texts(case_id=case_id, limit=limit)
    repo = repository(request)
    rows: list[tuple[object, str]] = []
    rows.extend(
        (draft.updated_at, draft.script)
        for draft in repo.drafts.values()
        if draft.case_id == case_id and draft.script.strip()
    )
    rows.extend(
        (script.updated_at, script.script)
        for script in repo.scripts.values()
        if script.case_id == case_id and script.script.strip()
    )
    rows.sort(key=lambda item: item[0], reverse=True)
    return _dedupe_texts([text for _, text in rows], limit=limit)


def _dedupe_texts(texts: list[str], *, limit: int) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for text in texts:
        normalized = " ".join(text.split()).strip()
        if not normalized:
            continue
        key = normalized[:100]
        if key in seen:
            continue
        seen.add(key)
        results.append(normalized)
        if len(results) >= limit:
            break
    return results


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
