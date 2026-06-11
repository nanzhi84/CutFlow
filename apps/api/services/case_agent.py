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
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError

def source_bindings(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseAgentSourceBinding]:

    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_source_bindings(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).source_bindings.values() if item.case_id == case_id], limit)


def create_source_binding(
    case_id: str, payload: c.CreateSourceBindingRequest, request: Request
) -> c.CaseAgentSourceBinding:
    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        return case_learning_repository(request).create_source_binding(case_id=case_id, payload=payload)
    binding = c.CaseAgentSourceBinding(id=new_id("src"), case_id=case_id, **payload.model_dump())
    repository(request).source_bindings[binding.id] = binding
    return binding


def delete_source_binding(case_id: str, binding_id: str, request: Request) -> c.OkResponse:
    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        deleted = case_learning_repository(request).delete_source_binding(case_id=case_id, binding_id=binding_id)
        if not deleted:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
        return c.OkResponse(ok=True, request_id=request_id())
    binding = repository(request).source_bindings.get(binding_id)
    if binding is None or binding.case_id != case_id:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
    del repository(request).source_bindings[binding_id]
    return c.OkResponse(ok=True, request_id=request_id())


def import_case_source(case_id: str, payload: c.ImportCaseSourceRequest, request: Request) -> c.CaseAgentRun:
    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        run = case_learning_repository(request).import_case_source(case_id=case_id, payload=payload)
        if run is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
        return run
    binding = repository(request).source_bindings.get(payload.source_binding_id)
    if binding is None or binding.case_id != case_id:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
    run = c.CaseAgentRun(
        id=new_id("agent_run"),
        case_id=case_id,
        goal="brief",
        status=c.RunStatus.succeeded,
        source_binding_ids=[payload.source_binding_id],
    )
    repository(request).case_agent_runs[run.id] = run
    brief = c.CreativeBrief(id=new_id("brief"), case_id=case_id, summary="Imported source summary.")
    repository(request).briefs[brief.id] = brief
    return run


def start_case_agent_run(
    case_id: str, payload: c.StartCaseAgentRunRequest, request: Request
) -> c.CaseAgentRun:
    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        return case_learning_repository(request).start_agent_run(case_id=case_id, payload=payload)
    run = c.CaseAgentRun(
        id=new_id("agent_run"),
        case_id=case_id,
        goal=payload.goal,
        status=c.RunStatus.succeeded,
        source_binding_ids=payload.source_binding_ids,
    )
    repository(request).case_agent_runs[run.id] = run
    if payload.goal == "script_draft":
        draft = c.ScriptDraft(
            id=new_id("draft"),
            case_id=case_id,
            title="Agent generated draft",
            script="开场提出痛点。展示解决方案。收束到行动建议。",
        )
        repository(request).drafts[draft.id] = draft
    if payload.goal == "memory_proposal":
        proposal = c.MemoryProposal(
            id=new_id("mem"),
            case_id=case_id,
            insight="Short hooks with concrete outcomes perform better for this case.",
            evidence=[],
            proposed_by_reflection_run_id=run.id,
        )
        repository(request).memory_proposals[proposal.id] = proposal
    return run


def case_agent_runs(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseAgentRun]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_agent_runs(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).case_agent_runs.values() if item.case_id == case_id], limit)


def case_agent_run_detail(request: Request, case_id: str, run_id: str) -> c.CaseAgentRunDetail:

    if case_learning_repository(request) is not None:
        detail = case_learning_repository(request).agent_run_detail(case_id=case_id, run_id=run_id)
        if detail is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Agent run is missing.")
        return detail
    run = repository(request).case_agent_runs[run_id]
    return c.CaseAgentRunDetail(
        run=run,
        briefs=[item for item in repository(request).briefs.values() if item.case_id == case_id],
        drafts=[item for item in repository(request).drafts.values() if item.case_id == case_id],
        memory_proposals=[item for item in repository(request).memory_proposals.values() if item.case_id == case_id],
    )


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
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Script draft is missing.")
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
    repository(request).drafts[draft.id] = draft.model_copy(update={"status": "adopted", "updated_at": c.utcnow()})
    return script


def memory_proposals(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.MemoryProposal]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_memory_proposals(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).memory_proposals.values() if item.case_id == case_id], limit)


def case_knowledge(request: Request, case_id: str) -> c.CaseKnowledgeResponse:

    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        return case_learning_repository(request).knowledge(case_id=case_id)
    return c.CaseKnowledgeResponse(
        case_id=case_id,
        memories=[item for item in repository(request).memories.values() if item.case_id == case_id],
        recent_script_versions=[item for item in repository(request).scripts.values() if item.case_id == case_id][-10:],
        recent_video_versions=[item for item in repository(request).video_versions.values() if item.case_id == case_id][-10:],
    )


def case_memory(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseMemory]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_memory(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).memories.values() if item.case_id == case_id], limit)


def approve_memory(
    case_id: str, memory_id: str, payload: c.ApproveMemoryRequest, request: Request
) -> c.CaseMemory:
    if case_learning_repository(request) is not None:
        memory = case_learning_repository(request).approve_memory(case_id=case_id, memory_id=memory_id)
        if memory is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Memory proposal is missing.")
        return memory
    proposal = repository(request).memory_proposals.get(memory_id) or repository(request).memories[memory_id]
    next_status = proposal.status
    if next_status == "proposed":
        assert_transition("case_memory", next_status, "approved")
        next_status = "approved"
    assert_transition("case_memory", next_status, "active")
    memory = c.CaseMemory.model_validate(
        proposal.model_dump(exclude={"proposed_by_reflection_run_id"})
    ).model_copy(update={"status": "active"})
    if memory_id in repository(request).memory_proposals:
        repository(request).memory_proposals[memory_id] = repository(request).memory_proposals[memory_id].model_copy(
            update={"status": "approved", "updated_at": c.utcnow()}
        )
    repository(request).memories[memory.id] = memory
    return memory


def reject_memory(
    case_id: str, memory_id: str, payload: c.RejectMemoryRequest, request: Request
) -> c.MemoryProposal:
    if case_learning_repository(request) is not None:
        proposal = case_learning_repository(request).reject_memory(case_id=case_id, memory_id=memory_id)
        if proposal is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Memory proposal is missing.")
        return proposal
    proposal = repository(request).memory_proposals[memory_id].model_copy(update={"status": "rejected"})
    repository(request).memory_proposals[memory_id] = proposal
    return proposal


def case_performance(request: Request, case_id: str, window: str = "7d") -> c.CasePerformanceResponse:

    if production_repository(request) is not None:
        return production_repository(request).case_performance(case_id=case_id, window=window)
    observations = [item for item in repository(request).performance_observations.values() if item.case_id == case_id]
    metrics = c.PerformanceMetricView(
        impressions=int(sum(item.metric_value for item in observations if item.metric_name == "impressions")),
        views=int(sum(item.metric_value for item in observations if item.metric_name == "views")),
        likes=int(sum(item.metric_value for item in observations if item.metric_name == "likes")),
    )
    return c.CasePerformanceResponse(metrics=metrics, observations=observations)


def import_metrics(case_id: str, payload: c.MetricsImportRequest, request: Request) -> c.ImportBatchReport:
    if production_repository(request) is not None:
        return production_repository(request).import_metrics(
            case_id=case_id,
            payload=payload,
            request_id=request_id(),
        )
    rows = []
    for index, row in enumerate(payload.rows):
        if isinstance(row, dict):
            obs = c.PerformanceObservation(
                id=new_id("perf"),
                case_id=case_id,
                publish_record_id=str(row.get("publish_record_id", "manual")),
                metric_name=str(row.get("metric_name", "views")),
                metric_value=float(row.get("metric_value", 0)),
            )
            if not payload.dry_run:
                repository(request).performance_observations[obs.id] = obs
            rows.append(c.ImportRowResult(row_index=index, status="created", internal_id=obs.id))
    report = c.ImportBatchReport(
        batch_id=new_id("imp"),
        import_type="performance",
        status=c.ImportBatchStatus.completed,
        created_count=len(rows),
        skipped_count=0,
        failed_count=0,
        results=rows,
        request_id=request_id(),
    )
    repository(request).import_reports[report.batch_id] = report
    return report


def start_reflection(case_id: str, payload: c.StartReflectionRunRequest, request: Request) -> c.ReflectionRun:
    if case_learning_repository(request) is not None:
        get_case(request, case_id)
        return case_learning_repository(request).start_reflection(case_id=case_id, payload=payload)
    reflection = c.ReflectionRun(
        id=new_id("refl"),
        case_id=case_id,
        status=c.RunStatus.succeeded,
        window=payload.window,
    )
    repository(request).reflection_runs[reflection.id] = reflection
    proposal = c.MemoryProposal(
        id=new_id("mem"),
        case_id=case_id,
        insight="Reuse the best performing hook style from recent videos.",
        evidence=[reflection.id],
        confidence=0.65,
        proposed_by_reflection_run_id=reflection.id,
    )
    repository(request).memory_proposals[proposal.id] = proposal
    return reflection


def case_insights(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseInsightCard]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).insights(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    cards = [
        c.CaseInsightCard(
            id=new_id("insight"),
            case_id=case_id,
            title="Memory proposals",
            body=f"{len([item for item in repository(request).memory_proposals.values() if item.case_id == case_id])} proposal(s) waiting for review.",
        )
    ]
    performance_count = len(
        [item for item in repository(request).performance_observations.values() if item.case_id == case_id]
    )
    if performance_count:
        cards.append(
            c.CaseInsightCard(
                id=new_id("insight"),
                case_id=case_id,
                title="Performance imports",
                body=f"{performance_count} performance observation(s) available for analysis.",
                severity="success",
            )
        )
    return page(cards, limit)


def creative_patterns(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CreativePattern]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).creative_patterns(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    patterns = [item for item in repository(request).creative_patterns.values() if item.case_id == case_id]
    if not patterns:
        patterns = [
            c.CreativePattern(
                id=new_id("pattern"),
                case_id=case_id,
                label="Concrete hook + short CTA",
                lift=None,
                evidence_count=len(repository(request).finished_videos),
            )
        ]
    return page(patterns, limit)


def generate_script_with_memory(
    case_id: str, payload: c.GenerateScriptWithMemoryRequest, request: Request
) -> c.ScriptDraft:
    repo = repository(request)
    if case_learning_repository(request) is not None:
        get_case(request, case_id)
        knowledge = case_learning_repository(request).knowledge(case_id=case_id)
        memories = [memory.insight for memory in knowledge.memories if memory.id in payload.memory_ids]
        provider_script = generate_script_with_llm(case_id, payload.brief, payload.memory_ids, memories, request)
        return case_learning_repository(request).generate_script_with_memory(
            case_id=case_id,
            payload=payload,
            script_override=provider_script,
        )
    memories = [repo.memories[mid].insight for mid in payload.memory_ids if mid in repo.memories]
    provider_script = generate_script_with_llm(case_id, payload.brief, payload.memory_ids, memories, request)
    draft = c.ScriptDraft(
        id=new_id("draft"),
        case_id=case_id,
        title="Memory-guided draft",
        script=provider_script or f"{payload.brief}\n\n参考记忆：{' / '.join(memories) if memories else '暂无'}",
        memory_ids=payload.memory_ids,
    )
    repo.drafts[draft.id] = draft
    return draft
