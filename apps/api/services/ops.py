from __future__ import annotations

from datetime import datetime

from fastapi import Request

from apps.api.common import (
    ops_repository,
    request_id,
)
from packages.core import contracts as c


def ops_dashboard(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    owner_user_id: str | None = None,
) -> c.OpsDashboardVm:
    return ops_repository(request).dashboard(
        window_start=window_start, window_end=window_end, owner_user_id=owner_user_id
    )


def cost_rollups(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    group_by: str | None = None,
    limit: int = 50,
) -> c.PageResponse[c.CostRollup]:
    values = ops_repository(request).list_cost_rollups(
        window_start=window_start,
        window_end=window_end,
        group_by=group_by,
        limit=limit,
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def provider_usage_metrics(request: Request, window_hours: int = 24) -> c.ProviderUsageMetricsReport:
    hours = max(1, min(window_hours, 24 * 30))
    return ops_repository(request).provider_usage_metrics(window_hours=hours, request_id=request_id())


def yield_funnel(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    case_id: str | None = None,
    owner_user_id: str | None = None,
) -> c.YieldFunnelResponse:
    return ops_repository(request).yield_funnel(
        window_start=window_start,
        window_end=window_end,
        case_id=case_id,
        owner_user_id=owner_user_id,
    )


def cost_metrics(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> c.CostMetrics:
    return ops_repository(request).cost_metrics(
        window_start=window_start, window_end=window_end
    )


def failure_taxonomy(
    request: Request,
    failure_class: str | None = None,
    run_id: str | None = None,
    case_id: str | None = None,
    limit: int = 50,
) -> c.PageResponse[c.FailureTaxonomyEntry]:
    values = ops_repository(request).list_failures(
        failure_class=failure_class, run_id=run_id, case_id=case_id, limit=limit
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def failure_analysis(request: Request) -> c.FailureAnalysisReport:
    return ops_repository(request).failure_analysis()


def list_alert_rules(request: Request, limit: int = 50) -> c.PageResponse[c.OpsAlertRule]:
    values = ops_repository(request).list_alert_rules(limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def upsert_alert_rule(payload: c.UpsertAlertRuleRequest, request: Request) -> c.OpsAlertRule:
    return ops_repository(request).upsert_alert_rule(payload)


def patch_alert_rule(rule_id: str, payload: c.PatchAlertRuleRequest, request: Request) -> c.OpsAlertRule:
    return ops_repository(request).patch_alert_rule(rule_id, payload)


def budgets(request: Request, limit: int = 50) -> c.PageResponse[c.Budget]:
    values = ops_repository(request).list_budgets(limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def upsert_budget(payload: c.UpsertBudgetRequest, request: Request) -> c.Budget:
    return ops_repository(request).upsert_budget(payload)


def patch_budget(budget_id: str, payload: c.PatchBudgetRequest, request: Request) -> c.Budget:
    return ops_repository(request).patch_budget(budget_id, payload)


def ack_alert(event_id: str, payload: c.AcknowledgeAlertRequest, request: Request) -> c.OpsAlertEvent:
    return ops_repository(request).patch_alert_status(event_id, "acknowledged")


def resolve_alert(event_id: str, payload: c.ResolveAlertRequest, request: Request) -> c.OpsAlertEvent:
    return ops_repository(request).patch_alert_status(event_id, "resolved")


def _qc_run_ids(repo, *, target_type: str, target_id: str) -> tuple[str | None, str | None]:
    """Best-effort (run_id, job_id) for a quality-check target so qc_* funnel
    events stay run-scoped. A ``run`` target IS the run; a ``finished_video``
    target resolves through the finished video's ``run_id``."""

    if target_type == "run":
        run = repo.runs.get(target_id)
        return (target_id if run is not None else None), getattr(run, "job_id", None)
    finished = repo.finished_videos.get(target_id) if target_id else None
    run_id = getattr(finished, "run_id", None) if finished else None
    run = repo.runs.get(run_id) if run_id else None
    return run_id, getattr(run, "job_id", None)


def run_quality_check(
    run_id: str, payload: c.CreateQualityCheckRequest, request: Request
) -> c.ProductionQualityCheck:
    return ops_repository(request).create_quality_check(
        target_type="run",
        target_id=run_id,
        payload=payload,
    )


def finished_video_quality_check(
    id: str, payload: c.CreateQualityCheckRequest, request: Request
) -> c.ProductionQualityCheck:
    return ops_repository(request).create_quality_check(
        target_type="finished_video",
        target_id=id,
        payload=payload,
    )


def approve_request(id: str, payload: c.ApprovalDecisionRequest, request: Request) -> c.ApprovalRequest:
    return ops_repository(request).decide_approval(id, "approved", payload)


def reject_request(id: str, payload: c.ApprovalDecisionRequest, request: Request) -> c.ApprovalRequest:
    return ops_repository(request).decide_approval(id, "rejected", payload)


def audit_events(request: Request, limit: int = 50) -> c.PageResponse[c.AuditEvent]:
    values = ops_repository(request).list_audit_events(limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
