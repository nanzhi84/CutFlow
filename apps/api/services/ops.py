from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Request

from apps.api.common import (
    ops_repository,
    page,
    repository,
    request_id,
)
from packages.core import contracts as c
from packages.core.observability import compute_true_yield_rate, record_funnel_event
from packages.core.storage.repository import new_id
from packages.ops import (
    FunnelCounts,
    InvocationCost,
    SpendRecord,
    compute_cost_metrics,
    compute_yield_rates,
    evaluate_budget,
)
from apps.api.services import providers as provider_service


def _memory_events(
    request: Request, case_id: str | None = None, owner_user_id: str | None = None
) -> list:
    """The §9.5 funnel events visible to the in-memory backend, optionally filtered
    by the case_id that owns each event's run, and (spec §3) by owner — an event's
    owner is its run's job.created_by (admin passes owner_user_id=None, sees all;
    events whose owner can't be resolved are hidden from non-admins)."""

    repo = repository(request)
    events = []
    for event in repo.yield_events.values():
        run = repo.runs.get(getattr(event, "run_id", None) or "")
        if case_id is not None and not (run is not None and run.case_id == case_id):
            continue
        if owner_user_id is not None and _event_owner(repo, event, run) != owner_user_id:
            continue
        events.append(event)
    return events


def _event_owner(repo, event, run) -> str | None:
    """Creator-based isolation: an event's owner = its run's job.created_by, falling
    back to its job_id's created_by when the run is detached/unknown."""
    if run is not None:
        job = repo.jobs.get(run.job_id)
        if job is not None:
            return job.created_by
    job = repo.jobs.get(getattr(event, "job_id", None) or "")
    return job.created_by if job is not None else None


def _memory_run_prompt_versions(request: Request) -> dict[str, set[str]]:
    repo = repository(request)
    mapping: dict[str, set[str]] = {}
    for invocation in repo.provider_invocations.values():
        if invocation.run_id and invocation.prompt_version_id:
            mapping.setdefault(invocation.run_id, set()).add(invocation.prompt_version_id)
    return mapping


def _memory_provider_success_rate(request: Request) -> float | None:
    repo = repository(request)
    invocations = list(repo.provider_invocations.values())
    if not invocations:
        return None
    ok = sum(1 for inv in invocations if inv.status == c.ProviderStatus.succeeded)
    return ok / len(invocations)


def _memory_yield_rates(
    request: Request, case_id: str | None = None, owner_user_id: str | None = None
) -> c.YieldRates:
    return compute_yield_rates(
        _memory_events(request, case_id, owner_user_id),
        provider_success_rate=_memory_provider_success_rate(request),
        run_prompt_versions=_memory_run_prompt_versions(request),
    )


def _memory_cost_metrics(request: Request) -> c.CostMetrics:
    repo = repository(request)
    events = list(repo.yield_events.values())
    finished = {e.finished_video_id for e in events if e.event_type == "finished_video_created" and getattr(e, "finished_video_id", None)}
    finished_jobs = {e.job_id for e in events if e.event_type == "finished_video_created" and getattr(e, "job_id", None)}
    qc_passed = {(getattr(e, "finished_video_id", None) or e.run_id) for e in events if e.event_type == "qc_passed" and (getattr(e, "finished_video_id", None) or e.run_id)}
    published = {(getattr(e, "publish_package_id", None) or e.run_id) for e in events if e.event_type == "published" and (getattr(e, "publish_package_id", None) or e.run_id)}
    wasted_runs = {e.run_id for e in events if e.event_type in ("qc_failed", "manual_rejected") and e.run_id}

    invocations: list[InvocationCost] = []
    for inv in repo.provider_invocations.values():
        run = repo.runs.get(inv.run_id or "")
        invocations.append(
            InvocationCost(
                estimated_amount=inv.estimated_cost.amount if inv.estimated_cost else Decimal("0"),
                actual_amount=inv.actual_cost.amount if inv.actual_cost else None,
                currency=(inv.estimated_cost.currency if inv.estimated_cost else "CNY"),
                provider_id=inv.provider_id,
                model_id=inv.model_id,
                prompt_version_id=inv.prompt_version_id,
                run_id=inv.run_id,
                run_is_failed=(getattr(run, "status", None) == c.RunStatus.failed),
                run_is_retry=bool(getattr(run, "retry_of_run_id", None)),
                node_attempt=(inv.retry_count or 0) + 1,
            )
        )
    counts = FunnelCounts(
        finished_video_count=len(finished) or len(finished_jobs),
        qc_passed_count=len(qc_passed),
        published_count=len(published),
        wasted_run_ids=frozenset(wasted_runs),
    )
    return compute_cost_metrics(invocations, counts, currency="CNY")


def _memory_spend_records(request: Request) -> list[SpendRecord]:
    repo = repository(request)
    return [
        SpendRecord(
            amount=inv.estimated_cost.amount if inv.estimated_cost else Decimal("0"),
            currency=inv.estimated_cost.currency if inv.estimated_cost else "CNY",
            created_at=inv.created_at,
            provider_id=inv.provider_id,
            capability_id=inv.capability_id,
            case_id=inv.case_id,
        )
        for inv in repo.provider_invocations.values()
    ]


def _memory_budget_evaluations(request: Request) -> list[c.BudgetEvaluation]:
    repo = repository(request)
    now = datetime.now(timezone.utc)
    records = _memory_spend_records(request)
    return [
        evaluate_budget(budget, records, now=now)
        for budget in repo.budgets.values()
        if budget.enabled
    ]


def _memory_failure_analysis(request: Request) -> c.FailureAnalysisReport:
    repo = repository(request)
    counts: dict[str, int] = {}
    for entry in repo.failures.values():
        fc = entry.failure_class.value if hasattr(entry.failure_class, "value") else str(entry.failure_class)
        counts[fc] = counts.get(fc, 0) + 1
    items = [
        c.FailureAnalysisItem(failure_class=c.FailureClass(fc), count=n)
        for fc, n in counts.items()
        if fc in c.FailureClass._value2member_map_
    ]
    items.sort(key=lambda item: (-item.count, item.failure_class.value))
    return c.FailureAnalysisReport(items=items, total=sum(i.count for i in items))

def ops_dashboard(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    owner_user_id: str | None = None,
) -> c.OpsDashboardVm:
    if ops_repository(request) is not None:
        return ops_repository(request).dashboard(
            window_start=window_start, window_end=window_end, owner_user_id=owner_user_id
        )
    usage = provider_service.provider_usage(request, window_start=window_start, window_end=window_end)
    funnel = yield_funnel(
        request, window_start=window_start, window_end=window_end, owner_user_id=owner_user_id
    )
    # Refresh the in-memory cost rollups so the dashboard reflects current spend.
    cost_rollups(request, window_start=window_start, window_end=window_end)
    return c.OpsDashboardVm(
        usage=usage,
        yield_funnel=funnel,
        alerts=list(repository(request).alerts.values()),
        cost_rollups=list(repository(request).cost_rollups.values()),
        cost_metrics=_memory_cost_metrics(request),
        yield_rates=funnel.rates,
        budget_evaluations=_memory_budget_evaluations(request),
        failure_analysis=_memory_failure_analysis(request),
    )


def cost_rollups(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    group_by: str | None = None,
    limit: int = 50,
) -> c.PageResponse[c.CostRollup]:
    if ops_repository(request) is not None:
        values = ops_repository(request).list_cost_rollups(
            window_start=window_start,
            window_end=window_end,
            group_by=group_by,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    repo = repository(request)
    # §26.1: GROUP BY the requested dimension and emit one CostRollup per group_key.
    groups: dict[str, tuple[Decimal, int]] = {}
    for inv in repo.provider_invocations.values():
        key = _cost_group_key(repo, inv, group_by)
        amount, count = groups.get(key, (Decimal("0"), 0))
        amount += inv.estimated_cost.amount if inv.estimated_cost else Decimal("0")
        groups[key] = (amount, count + 1)
    if not groups:
        groups["all" if group_by is None else "unknown"] = (Decimal("0"), 0)
    for group_key, (amount, count) in groups.items():
        rollup_id = "cost_current_all" if group_by is None else f"cost_{group_by}_{group_key}"
        repo.cost_rollups[rollup_id] = c.CostRollup(
            id=rollup_id,
            group_key=group_key,
            group_by=group_by,
            estimated_cost=c.Money(amount=amount, currency="CNY"),
            invocations=count,
            window_start=window_start,
            window_end=window_end,
        )
    return page(repo.cost_rollups.values(), limit)


def _cost_group_key(repo, invocation, group_by: str | None) -> str:
    if group_by is None:
        return "all"
    if group_by == "case":
        return invocation.case_id or "unknown"
    if group_by == "provider":
        return invocation.provider_id or "unknown"
    if group_by == "model":
        return invocation.model_id or "unknown"
    if group_by == "prompt_version":
        return invocation.prompt_version_id or "unknown"
    if group_by == "run":
        return invocation.run_id or "unknown"
    if group_by == "job":
        run = repo.runs.get(invocation.run_id or "")
        return getattr(run, "job_id", None) or "unknown"
    return "unknown"


def _provider_usage_metrics_from_invocations(
    invocations: list[c.ProviderInvocation],
    *,
    window_hours: int,
) -> list[c.ProviderUsageMetricsItem]:
    window_start = c.utcnow() - timedelta(hours=window_hours)
    buckets: dict[tuple[str, str, str | None], dict[str, object]] = {}
    for invocation in invocations:
        if invocation.started_at < window_start:
            continue
        key = (invocation.provider_id, invocation.capability_id, invocation.model_id)
        bucket = buckets.setdefault(
            key,
            {
                "calls": 0,
                "success_count": 0,
                "amount": Decimal("0"),
                "currency": invocation.estimated_cost.currency if invocation.estimated_cost else "CNY",
            },
        )
        bucket["calls"] = int(bucket["calls"]) + 1
        if invocation.status == c.ProviderStatus.succeeded:
            bucket["success_count"] = int(bucket["success_count"]) + 1
        if invocation.estimated_cost is not None:
            bucket["amount"] = bucket["amount"] + invocation.estimated_cost.amount
            bucket["currency"] = invocation.estimated_cost.currency

    items: list[c.ProviderUsageMetricsItem] = []
    for (provider_id, capability_id, model_id), bucket in buckets.items():
        calls = int(bucket["calls"])
        success_count = int(bucket["success_count"])
        items.append(
            c.ProviderUsageMetricsItem(
                provider_id=provider_id,
                capability_id=capability_id,
                model_id=model_id,
                calls=calls,
                success_count=success_count,
                success_rate=(success_count / calls) if calls else 0,
                estimated_cost=c.Money(amount=bucket["amount"], currency=str(bucket["currency"])),
                window_hours=window_hours,
            )
        )
    return sorted(items, key=lambda item: (-item.calls, item.provider_id, item.capability_id, item.model_id or ""))


def provider_usage_metrics(request: Request, window_hours: int = 24) -> c.ProviderUsageMetricsReport:
    hours = max(1, min(window_hours, 24 * 30))
    if ops_repository(request) is not None:
        return ops_repository(request).provider_usage_metrics(window_hours=hours, request_id=request_id())
    return c.ProviderUsageMetricsReport(
        items=_provider_usage_metrics_from_invocations(
            list(repository(request).provider_invocations.values()),
            window_hours=hours,
        ),
        window_hours=hours,
        generated_at=c.utcnow(),
        request_id=request_id(),
    )


def yield_funnel(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    case_id: str | None = None,
    owner_user_id: str | None = None,
) -> c.YieldFunnelResponse:
    if ops_repository(request) is not None:
        return ops_repository(request).yield_funnel(
            window_start=window_start,
            window_end=window_end,
            case_id=case_id,
            owner_user_id=owner_user_id,
        )
    events = _memory_events(request, case_id, owner_user_id)
    # §9.5: true_yield_rate must be run-scoped, NOT successes/total_events (the
    # denominator inflates as the taxonomy grows). A run is true-yield only if it
    # reached ``published`` and was never ``qc_failed`` / ``manual_rejected``.
    rate = compute_true_yield_rate(events)
    rates = _memory_yield_rates(request, case_id, owner_user_id)
    return c.YieldFunnelResponse(events=events, true_yield_rate=rate, rates=rates)


def cost_metrics(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> c.CostMetrics:
    if ops_repository(request) is not None:
        return ops_repository(request).cost_metrics(
            window_start=window_start, window_end=window_end
        )
    return _memory_cost_metrics(request)


def failure_taxonomy(
    request: Request,
    failure_class: str | None = None,
    run_id: str | None = None,
    case_id: str | None = None,
    limit: int = 50,
) -> c.PageResponse[c.FailureTaxonomyEntry]:
    if ops_repository(request) is not None:
        values = ops_repository(request).list_failures(
            failure_class=failure_class, run_id=run_id, case_id=case_id, limit=limit
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    repo = repository(request)
    items = []
    for entry in repo.failures.values():
        fc = entry.failure_class.value if hasattr(entry.failure_class, "value") else str(entry.failure_class)
        if failure_class and fc != failure_class:
            continue
        if run_id and entry.run_id != run_id:
            continue
        if case_id and entry.case_id != case_id:
            continue
        items.append(entry)
    items = items[:limit]
    return c.PageResponse(items=items, total_hint=len(items), request_id=request_id())


def failure_analysis(request: Request) -> c.FailureAnalysisReport:
    if ops_repository(request) is not None:
        return ops_repository(request).failure_analysis()
    return _memory_failure_analysis(request)


def list_alert_rules(request: Request, limit: int = 50) -> c.PageResponse[c.OpsAlertRule]:
    if ops_repository(request) is not None:
        values = ops_repository(request).list_alert_rules(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    repo = repository(request)
    return c.PageResponse(
        items=list(repo.alert_rules.values())[:limit],
        total_hint=len(repo.alert_rules),
        request_id=request_id(),
    )


def upsert_alert_rule(payload: c.UpsertAlertRuleRequest, request: Request) -> c.OpsAlertRule:
    if ops_repository(request) is not None:
        return ops_repository(request).upsert_alert_rule(payload)
    repo = repository(request)
    repo.alert_rules[payload.rule.id] = payload.rule
    return payload.rule


def patch_alert_rule(rule_id: str, payload: c.PatchAlertRuleRequest, request: Request) -> c.OpsAlertRule:
    if ops_repository(request) is not None:
        return ops_repository(request).patch_alert_rule(rule_id, payload)
    repo = repository(request)
    return repo.patch(repo.alert_rules, rule_id, payload.model_dump(exclude_none=True))


def budgets(request: Request, limit: int = 50) -> c.PageResponse[c.Budget]:

    if ops_repository(request) is not None:
        values = ops_repository(request).list_budgets(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository(request).budgets.values(), limit)


def upsert_budget(payload: c.UpsertBudgetRequest, request: Request) -> c.Budget:
    if ops_repository(request) is not None:
        return ops_repository(request).upsert_budget(payload)
    repository(request).budgets[payload.budget.id] = payload.budget
    return payload.budget


def patch_budget(budget_id: str, payload: c.PatchBudgetRequest, request: Request) -> c.Budget:
    if ops_repository(request) is not None:
        return ops_repository(request).patch_budget(budget_id, payload)
    return repository(request).patch(repository(request).budgets, budget_id, payload.model_dump(exclude_none=True))


def ack_alert(event_id: str, payload: c.AcknowledgeAlertRequest, request: Request) -> c.OpsAlertEvent:
    if ops_repository(request) is not None:
        return ops_repository(request).patch_alert_status(event_id, "acknowledged")
    return repository(request).patch(repository(request).alerts, event_id, {"status": "acknowledged"})


def resolve_alert(event_id: str, payload: c.ResolveAlertRequest, request: Request) -> c.OpsAlertEvent:
    if ops_repository(request) is not None:
        return ops_repository(request).patch_alert_status(event_id, "resolved")
    return repository(request).patch(repository(request).alerts, event_id, {"status": "resolved"})


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


def _record_quality_check_funnel(repo, check: c.ProductionQualityCheck) -> None:
    """Emit the §9.5 qc_* stages for one quality check. Always records
    ``qc_started``; then ``qc_passed`` (result == passed) or ``qc_failed``
    (result == failed). ``warning`` / ``manual_required`` results emit only
    ``qc_started`` (the QC ran but did not terminally pass/fail). Best-effort —
    ``qc_failed`` disqualifies the run from true yield."""

    run_id, job_id = _qc_run_ids(repo, target_type=check.target_type, target_id=check.target_id)
    record_funnel_event(
        repo,
        event_type="qc_started",
        job_id=job_id,
        run_id=run_id,
        finished_video_id=check.target_id if check.target_type == "finished_video" else None,
        dedupe_key=f"{check.id}:qc_started",
        event_time=check.created_at,
    )
    result = check.result.value if hasattr(check.result, "value") else str(check.result)
    terminal = {"passed": "qc_passed", "failed": "qc_failed"}.get(result)
    if terminal is not None:
        record_funnel_event(
            repo,
            event_type=terminal,
            job_id=job_id,
            run_id=run_id,
            finished_video_id=check.target_id if check.target_type == "finished_video" else None,
            dedupe_key=f"{check.id}:{terminal}",
            event_time=check.created_at,
        )
    # §9.6: a failed QC is a terminal failure -> classify ``qc_failed``.
    if result == "failed":
        run = repo.runs.get(run_id or "")
        repo.record_failure_taxonomy(
            target_type=check.target_type,
            target_id=check.target_id,
            failure_class=c.FailureClass.qc_failed,
            run_id=run_id,
            job_id=job_id,
            case_id=getattr(run, "case_id", None),
            message=check.reason_code,
            dedupe_key=f"{check.id}:qc_failed",
        )


def _approval_run_ids(repo, approval: c.ApprovalRequest) -> tuple[str | None, str | None]:
    """Best-effort (run_id, job_id) for an approval decision so manual_* funnel
    events stay run-scoped. Resolves a run/finished_video resource_id to its run."""

    resource_id = getattr(approval, "resource_id", None)
    if not resource_id:
        return None, None
    if resource_id in repo.runs:
        return resource_id, getattr(repo.runs[resource_id], "job_id", None)
    finished = repo.finished_videos.get(resource_id)
    run_id = getattr(finished, "run_id", None) if finished else None
    run = repo.runs.get(run_id) if run_id else None
    return run_id, getattr(run, "job_id", None)


def _record_approval_funnel(repo, approval: c.ApprovalRequest, *, decision: str) -> None:
    """Emit the §9.5 manual_* stage for one approval decision: ``manual_approved``
    (approved) or ``manual_rejected`` (rejected). Best-effort — ``manual_rejected``
    disqualifies the run from true yield."""

    event_type = "manual_approved" if decision == "approved" else "manual_rejected"
    run_id, job_id = _approval_run_ids(repo, approval)
    record_funnel_event(
        repo,
        event_type=event_type,
        job_id=job_id,
        run_id=run_id,
        dedupe_key=f"{approval.id}:{event_type}",
        event_time=approval.updated_at,
    )
    # §9.6: a manual rejection is a terminal failure -> classify ``manual_rejected``.
    if decision == "rejected":
        run = repo.runs.get(run_id or "")
        repo.record_failure_taxonomy(
            target_type="run" if run_id else "approval_request",
            target_id=run_id or approval.id,
            failure_class=c.FailureClass.manual_rejected,
            run_id=run_id,
            job_id=job_id,
            case_id=getattr(run, "case_id", None),
            message=approval.reason,
            dedupe_key=f"{approval.id}:manual_rejected",
        )


def run_quality_check(
    run_id: str, payload: c.CreateQualityCheckRequest, request: Request
) -> c.ProductionQualityCheck:
    if ops_repository(request) is not None:
        return ops_repository(request).create_quality_check(
            target_type="run",
            target_id=run_id,
            payload=payload,
        )
    repo = repository(request)
    check = c.ProductionQualityCheck(id=new_id("qc"), target_type="run", target_id=run_id, **payload.model_dump())
    repo.quality_checks[check.id] = check
    _record_quality_check_funnel(repo, check)
    return check


def finished_video_quality_check(
    id: str, payload: c.CreateQualityCheckRequest, request: Request
) -> c.ProductionQualityCheck:
    if ops_repository(request) is not None:
        return ops_repository(request).create_quality_check(
            target_type="finished_video",
            target_id=id,
            payload=payload,
        )
    repo = repository(request)
    check = c.ProductionQualityCheck(
        id=new_id("qc"),
        target_type="finished_video",
        target_id=id,
        **payload.model_dump(),
    )
    repo.quality_checks[check.id] = check
    _record_quality_check_funnel(repo, check)
    return check


def approve_request(id: str, payload: c.ApprovalDecisionRequest, request: Request) -> c.ApprovalRequest:
    if ops_repository(request) is not None:
        return ops_repository(request).decide_approval(id, "approved", payload)
    repo = repository(request)
    existing = repo.approvals.get(id)
    approval = c.ApprovalRequest(
        id=id,
        resource_type=getattr(existing, "resource_type", None) or "approval_request",
        resource_id=getattr(existing, "resource_id", None) or id,
        status="approved",
        reason=payload.reason,
    )
    repo.approvals[id] = approval
    _record_approval_funnel(repo, approval, decision="approved")
    return approval


def reject_request(id: str, payload: c.ApprovalDecisionRequest, request: Request) -> c.ApprovalRequest:
    if ops_repository(request) is not None:
        return ops_repository(request).decide_approval(id, "rejected", payload)
    repo = repository(request)
    existing = repo.approvals.get(id)
    approval = c.ApprovalRequest(
        id=id,
        resource_type=getattr(existing, "resource_type", None) or "approval_request",
        resource_id=getattr(existing, "resource_id", None) or id,
        status="rejected",
        reason=payload.reason,
    )
    repo.approvals[id] = approval
    _record_approval_funnel(repo, approval, decision="rejected")
    return approval


def audit_events(request: Request, limit: int = 50) -> c.PageResponse[c.AuditEvent]:
    if ops_repository(request) is not None:
        values = ops_repository(request).list_audit_events(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository(request).audit_events.values(), limit)
