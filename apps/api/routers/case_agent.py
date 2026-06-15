from __future__ import annotations


from fastapi import APIRouter, Request

from apps.api.dependencies import require_role
from apps.api.services import case_agent as service
from packages.core import contracts as c

router = APIRouter()

@router.get(
    "/api/cases/{case_id}/agent/source-bindings",
    response_model=c.PageResponse[c.CaseAgentSourceBinding],
)
def source_bindings(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseAgentSourceBinding]:

    return service.source_bindings(request, case_id, limit)


@router.post(
    "/api/cases/{case_id}/agent/source-bindings",
    response_model=c.CaseAgentSourceBinding,
    status_code=201,
)
def create_source_binding(
    case_id: str, payload: c.CreateSourceBindingRequest, request: Request
) -> c.CaseAgentSourceBinding:
    require_role(request, c.UserRole.operator)
    return service.create_source_binding(case_id, payload, request)


@router.delete("/api/cases/{case_id}/agent/source-bindings/{binding_id}", response_model=c.OkResponse)
def delete_source_binding(case_id: str, binding_id: str, request: Request) -> c.OkResponse:
    require_role(request, c.UserRole.operator)
    return service.delete_source_binding(case_id, binding_id, request)


@router.post("/api/cases/{case_id}/agent/import-source", response_model=c.CaseAgentRun, status_code=202)
async def import_case_source(
    case_id: str, payload: c.ImportCaseSourceRequest, request: Request
) -> c.CaseAgentRun:
    require_role(request, c.UserRole.operator)
    return await service.import_case_source(case_id, payload, request)


@router.post("/api/cases/{case_id}/agent/runs", response_model=c.CaseAgentRun, status_code=202)
def start_case_agent_run(
    case_id: str, payload: c.StartCaseAgentRunRequest, request: Request
) -> c.CaseAgentRun:
    require_role(request, c.UserRole.operator)
    return service.start_case_agent_run(case_id, payload, request)


@router.get("/api/cases/{case_id}/agent/runs", response_model=c.PageResponse[c.CaseAgentRun])
def case_agent_runs(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseAgentRun]:

    return service.case_agent_runs(request, case_id, limit)


@router.get("/api/cases/{case_id}/agent/runs/{run_id}", response_model=c.CaseAgentRunDetail)
def case_agent_run_detail(request: Request, case_id: str, run_id: str) -> c.CaseAgentRunDetail:

    return service.case_agent_run_detail(request, case_id, run_id)


@router.get("/api/cases/{case_id}/agent/drafts", response_model=c.PageResponse[c.ScriptDraft])
def script_drafts(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.ScriptDraft]:

    return service.script_drafts(request, case_id, limit)


@router.post(
    "/api/cases/{case_id}/agent/drafts/{draft_id}/adopt",
    response_model=c.ScriptVersion,
    status_code=201,
)
def adopt_script_draft(
    case_id: str, draft_id: str, payload: c.AdoptScriptDraftRequest, request: Request
) -> c.ScriptVersion:
    require_role(request, c.UserRole.operator)
    return service.adopt_script_draft(case_id, draft_id, payload, request)


@router.get("/api/cases/{case_id}/agent/memory-proposals", response_model=c.PageResponse[c.MemoryProposal])
def memory_proposals(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.MemoryProposal]:

    return service.memory_proposals(request, case_id, limit)


@router.get("/api/cases/{case_id}/knowledge", response_model=c.CaseKnowledgeResponse)
def case_knowledge(request: Request, case_id: str) -> c.CaseKnowledgeResponse:

    return service.case_knowledge(request, case_id)


@router.get("/api/cases/{case_id}/memory", response_model=c.PageResponse[c.CaseMemory])
def case_memory(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseMemory]:

    return service.case_memory(request, case_id, limit)


@router.get("/api/cases/{case_id}/memory/recall", response_model=c.MemoryRecallResponse)
def recall_memory(
    request: Request,
    case_id: str,
    mode: c.MemoryRecallMode = "recent",
    topic: str | None = None,
    platform: str | None = None,
    memory_type: c.MemoryType | None = None,
    scope_key: str | None = None,
    limit: int = 20,
) -> c.MemoryRecallResponse:
    query = c.MemoryRecallQuery(
        mode=mode,
        topic=topic,
        platform=platform,
        memory_type=memory_type,
        scope_key=scope_key,
        limit=limit,
    )
    return service.recall_memory(request, case_id, query)


@router.post("/api/cases/{case_id}/memory/{memory_id}/approve", response_model=c.CaseMemory)
def approve_memory(
    case_id: str, memory_id: str, payload: c.ApproveMemoryRequest, request: Request
) -> c.CaseMemory:
    require_role(request, c.UserRole.operator)
    return service.approve_memory(case_id, memory_id, payload, request)


@router.post("/api/cases/{case_id}/memory/{memory_id}/reject", response_model=c.MemoryProposal)
def reject_memory(
    case_id: str, memory_id: str, payload: c.RejectMemoryRequest, request: Request
) -> c.MemoryProposal:
    require_role(request, c.UserRole.operator)
    return service.reject_memory(case_id, memory_id, payload, request)


@router.get("/api/cases/{case_id}/performance", response_model=c.CasePerformanceResponse)
def case_performance(request: Request, case_id: str, window: str = "7d") -> c.CasePerformanceResponse:

    return service.case_performance(request, case_id, window)


@router.post("/api/cases/{case_id}/metrics/import", response_model=c.ImportBatchReport, status_code=202)
def import_metrics(case_id: str, payload: c.MetricsImportRequest, request: Request) -> c.ImportBatchReport:
    require_role(request, c.UserRole.operator)
    return service.import_metrics(case_id, payload, request)


@router.post("/api/cases/{case_id}/reflection-runs", response_model=c.ReflectionRun, status_code=202)
def start_reflection(case_id: str, payload: c.StartReflectionRunRequest, request: Request) -> c.ReflectionRun:
    require_role(request, c.UserRole.operator)
    return service.start_reflection(case_id, payload, request)


@router.get("/api/cases/{case_id}/insights", response_model=c.PageResponse[c.CaseInsightCard])
def case_insights(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseInsightCard]:

    return service.case_insights(request, case_id, limit)


@router.get("/api/cases/{case_id}/creative-patterns", response_model=c.PageResponse[c.CreativePattern])
def creative_patterns(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CreativePattern]:

    return service.creative_patterns(request, case_id, limit)


@router.post("/api/cases/{case_id}/scripts/generate-with-memory", response_model=c.ScriptDraft, status_code=202)
def generate_script_with_memory(
    case_id: str, payload: c.GenerateScriptWithMemoryRequest, request: Request
) -> c.ScriptDraft:
    require_role(request, c.UserRole.operator)
    return service.generate_script_with_memory(case_id, payload, request)
