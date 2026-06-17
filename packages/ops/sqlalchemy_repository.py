from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.sqltypes import Numeric

from packages.core.contracts import (
    ApprovalDecisionRequest,
    ApprovalRequest,
    AuditEvent,
    Budget,
    BudgetEvaluation,
    CostMetrics,
    CostRollup,
    CreateQualityCheckRequest,
    ErrorCode,
    FailureAnalysisItem,
    FailureAnalysisReport,
    FailureClass,
    FailureTaxonomyEntry,
    Money,
    OpsAlertEvent,
    OpsAlertRule,
    OpsDashboardVm,
    PatchAlertRuleRequest,
    PatchBudgetRequest,
    ProductionQualityCheck,
    ReconcileBillingLineItem,
    ProviderUsageReport,
    ProviderUsageMetricsReport,
    ReconcileBillingRequest,
    ReconcileBillingResponse,
    UpsertAlertRuleRequest,
    UpsertBudgetRequest,
    YieldFunnelResponse,
    utcnow,
)
from packages.core.storage.database import (
    ApprovalRequestRow,
    AuditEventRow,
    BudgetRow,
    CostRollupRow,
    FailureTaxonomyRow,
    FinishedVideoRow,
    OpsAlertEventRow,
    OpsAlertRuleRow,
    ProviderBalanceSnapshotRow,
    ProviderBillingReconciliationRow,
    ProviderInvocationRow,
    ProviderPriceItemRow,
    ProductionQualityCheckRow,
    UsageMeterRecordRow,
    WorkflowRunRow,
    YieldFunnelEventRow,
)
from packages.core.observability import compute_true_yield_rate, persist_funnel_event_rows
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.ops.budget_evaluation import SpendRecord, evaluate_budget
from packages.ops.cost_metrics import FunnelCounts, InvocationCost, compute_cost_metrics
from packages.ops.failure_taxonomy import classify_error_code
from packages.ops.sqlalchemy_mappers import (
    alert_row_to_contract,
    alert_rule_row_to_contract,
    approval_row_to_contract,
    audit_row_to_contract,
    budget_row_to_contract,
    cost_rollup_row_to_contract,
    failure_taxonomy_row_to_contract,
    quality_check_row_to_contract,
    yield_event_row_to_contract,
)
from packages.ops.provider_usage_metrics import sqlalchemy_provider_usage_metrics
from packages.ops.yield_rates import compute_yield_rates


_NUMERIC = Numeric(20, 6)


def _money_amount(payload: dict | None) -> Decimal:
    if not payload:
        return Decimal("0")
    try:
        return Decimal(str(payload.get("amount", "0")))
    except (TypeError, ValueError):
        return Decimal("0")


def _optional_money_amount(payload: dict | None) -> Decimal | None:
    if not payload:
        return None
    return _money_amount(payload)


def _money_currency(payload: dict | None) -> str:
    if not payload:
        return "CNY"
    return str(payload.get("currency") or "CNY")


class SqlAlchemyOpsRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def dashboard(self, window_start: datetime | None = None, window_end: datetime | None = None) -> OpsDashboardVm:
        funnel = self.yield_funnel(window_start=window_start, window_end=window_end)
        return OpsDashboardVm(
            usage=self.provider_usage(window_start=window_start, window_end=window_end),
            yield_funnel=funnel,
            alerts=self.list_alerts(),
            cost_rollups=self.list_cost_rollups(window_start=window_start, window_end=window_end, limit=50),
            cost_metrics=self.cost_metrics(window_start=window_start, window_end=window_end),
            yield_rates=funnel.rates,
            budget_evaluations=self.evaluate_budgets(),
            failure_analysis=self.failure_analysis(window_start=window_start, window_end=window_end),
        )

    def provider_usage(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        provider_id: str | None = None,
        case_id: str | None = None,
    ) -> ProviderUsageReport:
        with self.session_factory() as session:
            statement = select(ProviderInvocationRow)
            if provider_id:
                statement = statement.where(ProviderInvocationRow.provider_id == provider_id)
            if case_id:
                statement = statement.where(ProviderInvocationRow.case_id == case_id)
            if window_start:
                statement = statement.where(ProviderInvocationRow.created_at >= window_start)
            if window_end:
                statement = statement.where(ProviderInvocationRow.created_at <= window_end)
            rows = list(session.scalars(statement))
        estimated = sum(
            (
                Money.model_validate(row.estimated_cost).amount
                for row in rows
                if row.estimated_cost is not None
            ),
            Money(amount=0, currency="CNY").amount,
        )
        unpriced = len([row for row in rows if row.billing_status == "unpriced"])
        return ProviderUsageReport(
            invocations=len(rows),
            estimated_cost=Money(amount=estimated, currency="CNY"),
            unpriced_invocation_count=unpriced,
        )

    def provider_usage_metrics(self, *, window_hours: int = 24, request_id: str) -> ProviderUsageMetricsReport:
        return sqlalchemy_provider_usage_metrics(
            self.session_factory,
            window_hours=window_hours,
            request_id=request_id,
        )

    # §26.1: the column on ProviderInvocationRow each cost-rollup dimension groups by.
    _GROUP_BY_COLUMN = {
        "case": ProviderInvocationRow.case_id,
        "provider": ProviderInvocationRow.provider_id,
        "model": ProviderInvocationRow.model_id,
        "prompt_version": ProviderInvocationRow.prompt_version_id,
        "run": ProviderInvocationRow.run_id,
        "job": None,  # resolved via run_id -> workflow_runs.job_id
    }

    def list_cost_rollups(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        group_by: str | None = None,
        limit: int = 50,
    ) -> list[CostRollup]:
        """§26.1: emit ONE CostRollup per group_key (real SQL GROUP BY), not a
        single aggregate row whose group_key is the dimension name. ``group_by``
        is None -> a single ``cost_current_all`` row (overall total)."""

        now = utcnow()
        groups = self._aggregate_cost_groups(
            group_by=group_by, window_start=window_start, window_end=window_end
        )
        with self.session_factory() as session:
            for group_key, (amount, count) in groups.items():
                rollup_id = (
                    "cost_current_all"
                    if group_by is None
                    else f"cost_{group_by}_{group_key}"
                )
                estimated = Money(amount=amount, currency="CNY")
                row = session.get(CostRollupRow, rollup_id)
                if row is None:
                    row = CostRollupRow(
                        id=rollup_id,
                        group_key=group_key,
                        group_by=group_by,
                        estimated_cost=estimated.model_dump(mode="json"),
                        actual_cost=None,
                        invocations=count,
                        window_start=window_start,
                        window_end=window_end,
                    )
                    session.add(row)
                else:
                    row.group_key = group_key
                    row.group_by = group_by
                    row.estimated_cost = estimated.model_dump(mode="json")
                    row.invocations = count
                    row.window_start = window_start
                    row.window_end = window_end
                    row.updated_at = now
            session.commit()
            statement = (
                select(CostRollupRow)
                .order_by(CostRollupRow.updated_at.desc())
                .limit(limit)
            )
            return [cost_rollup_row_to_contract(item) for item in session.scalars(statement)]

    def _aggregate_cost_groups(
        self,
        *,
        group_by: str | None,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> dict[str, tuple[Decimal, int]]:
        """Return {group_key: (estimated_cost_sum, invocation_count)} grouped by the
        requested §26.1 dimension. ``group_by`` None -> a single ``all`` bucket."""

        amount = ProviderInvocationRow.estimated_cost["amount"].astext.cast(_NUMERIC)
        groups: dict[str, tuple[Decimal, int]] = {}
        with self.session_factory() as session:
            if group_by is None:
                statement = select(
                    func.coalesce(func.sum(amount), 0),
                    func.count(ProviderInvocationRow.id),
                )
                statement = self._apply_cost_window(statement, window_start, window_end)
                total, count = session.execute(statement).one()
                groups["all"] = (Decimal(str(total or 0)), int(count or 0))
                return groups

            if group_by == "job":
                key_col = WorkflowRunRow.job_id
                statement = (
                    select(
                        key_col,
                        func.coalesce(func.sum(amount), 0),
                        func.count(ProviderInvocationRow.id),
                    )
                    .join(
                        WorkflowRunRow,
                        WorkflowRunRow.id == ProviderInvocationRow.run_id,
                    )
                    .group_by(key_col)
                )
            else:
                key_col = self._GROUP_BY_COLUMN[group_by]
                statement = select(
                    key_col,
                    func.coalesce(func.sum(amount), 0),
                    func.count(ProviderInvocationRow.id),
                ).group_by(key_col)
            statement = self._apply_cost_window(statement, window_start, window_end)
            for key, total, count in session.execute(statement):
                groups[str(key) if key is not None else "unknown"] = (
                    Decimal(str(total or 0)),
                    int(count or 0),
                )
        return groups

    @staticmethod
    def _apply_cost_window(statement, window_start, window_end):
        if window_start:
            statement = statement.where(ProviderInvocationRow.created_at >= window_start)
        if window_end:
            statement = statement.where(ProviderInvocationRow.created_at <= window_end)
        return statement

    def cost_metrics(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> CostMetrics:
        """§9.4 / §26.2 cost indicators: join provider_invocations cost to §9.5
        funnel counts (finished/qc_passed/published) and run/node state."""

        with self.session_factory() as session:
            inv_statement = select(
                ProviderInvocationRow.estimated_cost,
                ProviderInvocationRow.actual_cost,
                ProviderInvocationRow.provider_id,
                ProviderInvocationRow.model_id,
                ProviderInvocationRow.prompt_version_id,
                ProviderInvocationRow.run_id,
                ProviderInvocationRow.retry_count,
                WorkflowRunRow.status,
                WorkflowRunRow.retry_of_run_id,
            ).outerjoin(WorkflowRunRow, WorkflowRunRow.id == ProviderInvocationRow.run_id)
            inv_statement = self._apply_cost_window(inv_statement, window_start, window_end)
            rows = list(session.execute(inv_statement))

            funnel_statement = select(YieldFunnelEventRow)
            if window_start:
                funnel_statement = funnel_statement.where(
                    YieldFunnelEventRow.created_at >= window_start
                )
            if window_end:
                funnel_statement = funnel_statement.where(
                    YieldFunnelEventRow.created_at <= window_end
                )
            events = list(session.scalars(funnel_statement))

        finished = {e.finished_video_id for e in events if e.event_type == "finished_video_created" and e.finished_video_id}
        finished_jobs = {e.job_id for e in events if e.event_type == "finished_video_created" and e.job_id}
        qc_passed = {(e.finished_video_id or e.run_id) for e in events if e.event_type == "qc_passed" and (e.finished_video_id or e.run_id)}
        published = {(e.publish_package_id or e.run_id) for e in events if e.event_type == "published" and (e.publish_package_id or e.run_id)}
        wasted_runs = {e.run_id for e in events if e.event_type in ("qc_failed", "manual_rejected") and e.run_id}

        invocations = [
            InvocationCost(
                estimated_amount=_money_amount(estimated_cost),
                actual_amount=_optional_money_amount(actual_cost),
                currency=_money_currency(estimated_cost),
                provider_id=provider_id,
                model_id=model_id,
                prompt_version_id=prompt_version_id,
                run_id=run_id,
                run_is_failed=(status == "failed"),
                run_is_retry=bool(retry_of_run_id),
                node_attempt=(retry_count or 0) + 1,
            )
            for (
                estimated_cost,
                actual_cost,
                provider_id,
                model_id,
                prompt_version_id,
                run_id,
                retry_count,
                status,
                retry_of_run_id,
            ) in rows
        ]
        counts = FunnelCounts(
            finished_video_count=len(finished) or len(finished_jobs),
            qc_passed_count=len(qc_passed),
            published_count=len(published),
            wasted_run_ids=frozenset(wasted_runs),
        )
        return compute_cost_metrics(
            invocations,
            counts,
            currency="CNY",
            window_start=window_start,
            window_end=window_end,
        )

    def yield_funnel(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        case_id: str | None = None,
    ) -> YieldFunnelResponse:
        with self.session_factory() as session:
            statement = select(YieldFunnelEventRow)
            if case_id:
                statement = statement.where(YieldFunnelEventRow.case_id == case_id)
            if window_start:
                statement = statement.where(YieldFunnelEventRow.created_at >= window_start)
            if window_end:
                statement = statement.where(YieldFunnelEventRow.created_at <= window_end)
            events = [yield_event_row_to_contract(row) for row in session.scalars(statement)]
            run_prompt_versions = self._run_prompt_versions(session, [e.run_id for e in events if e.run_id])
        # §9.5: true_yield_rate is run-scoped (distinct runs that reached
        # ``published`` and were never ``qc_failed`` / ``manual_rejected``) over
        # distinct runs that entered the funnel — NOT successes/total_events,
        # which inflates as the taxonomy grows.
        rate = compute_true_yield_rate(events)
        rates = compute_yield_rates(
            events,
            provider_success_rate=self._provider_success_rate(window_start, window_end),
            run_prompt_versions=run_prompt_versions,
        )
        return YieldFunnelResponse(events=events, true_yield_rate=rate, rates=rates)

    def _run_prompt_versions(self, session: Session, run_ids: list[str]) -> dict[str, set[str]]:
        """Map run_id -> {prompt_version_id} via provider_invocations, for
        §26.3 prompt_version_yield."""

        if not run_ids:
            return {}
        statement = select(
            ProviderInvocationRow.run_id, ProviderInvocationRow.prompt_version_id
        ).where(
            ProviderInvocationRow.run_id.in_(set(run_ids)),
            ProviderInvocationRow.prompt_version_id.isnot(None),
        )
        mapping: dict[str, set[str]] = defaultdict(set)
        for run_id, version_id in session.execute(statement):
            if run_id and version_id:
                mapping[run_id].add(version_id)
        return dict(mapping)

    def _provider_success_rate(
        self, window_start: datetime | None, window_end: datetime | None
    ) -> float | None:
        """Overall provider success rate (succeeded invocations / total) for the
        window — the §9.5 provider_success_rate surfaced alongside the yield set."""

        with self.session_factory() as session:
            total_stmt = select(func.count(ProviderInvocationRow.id))
            total_stmt = self._apply_cost_window(total_stmt, window_start, window_end)
            total = session.scalar(total_stmt) or 0
            if total == 0:
                return None
            ok_stmt = select(func.count(ProviderInvocationRow.id)).where(
                ProviderInvocationRow.status == "succeeded"
            )
            ok_stmt = self._apply_cost_window(ok_stmt, window_start, window_end)
            ok = session.scalar(ok_stmt) or 0
        return ok / total

    def list_budgets(self, *, limit: int = 50) -> list[Budget]:
        with self.session_factory() as session:
            statement = select(BudgetRow).order_by(BudgetRow.updated_at.desc()).limit(limit)
            return [budget_row_to_contract(row) for row in session.scalars(statement)]

    def upsert_budget(self, payload: UpsertBudgetRequest) -> Budget:
        budget = payload.budget
        with self.session_factory() as session:
            row = BudgetRow(
                id=budget.id,
                scope_type=budget.scope_type,
                scope_id=budget.scope_id,
                period=budget.period,
                limit=budget.limit.model_dump(mode="json"),
                alert_threshold=budget.alert_threshold,
                enabled=budget.enabled,
                enforce=budget.enforce,
                schema_version=budget.schema_version,
                created_at=budget.created_at,
                updated_at=utcnow(),
            )
            merged = session.merge(row)
            session.commit()
            session.refresh(merged)
            return budget_row_to_contract(merged)

    def patch_budget(self, budget_id: str, payload: PatchBudgetRequest) -> Budget:
        with self.session_factory() as session:
            row = session.get(BudgetRow, budget_id)
            if row is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Budget not found.")
            updates = payload.model_dump(exclude_none=True)
            if "limit" in updates:
                row.limit = payload.limit.model_dump(mode="json") if payload.limit else row.limit
            if "alert_threshold" in updates:
                row.alert_threshold = updates["alert_threshold"]
            if "enabled" in updates:
                row.enabled = updates["enabled"]
            if "enforce" in updates:
                row.enforce = updates["enforce"]
            if "period" in updates:
                row.period = updates["period"]
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return budget_row_to_contract(row)

    def evaluate_budgets(self, *, now: datetime | None = None) -> list[BudgetEvaluation]:
        """§9.8 预算执行: evaluate every enabled budget against current-period spend
        and (best-effort) raise/clear a ``budget.exceeded`` alert on crossing."""

        now = now or utcnow()
        budgets = [b for b in self.list_budgets(limit=200) if b.enabled]
        if not budgets:
            return []
        spend_records = self._spend_records()
        evaluations = [evaluate_budget(b, spend_records, now=now) for b in budgets]
        self._sync_budget_alerts(evaluations)
        return evaluations

    def _spend_records(self) -> list[SpendRecord]:
        with self.session_factory() as session:
            statement = select(
                ProviderInvocationRow.estimated_cost,
                ProviderInvocationRow.created_at,
                ProviderInvocationRow.provider_id,
                ProviderInvocationRow.capability_id,
                ProviderInvocationRow.case_id,
            )
            rows = list(session.execute(statement))
        return [
            SpendRecord(
                amount=_money_amount(estimated_cost),
                currency=_money_currency(estimated_cost),
                created_at=created_at,
                provider_id=provider_id,
                capability_id=capability_id,
                case_id=case_id,
            )
            for estimated_cost, created_at, provider_id, capability_id, case_id in rows
        ]

    def _sync_budget_alerts(self, evaluations: list[BudgetEvaluation]) -> None:
        """Open a deterministic ``budget.exceeded`` alert per over-threshold budget
        and resolve it once spend falls back below threshold. Best-effort — never
        raises into the dashboard read path."""

        try:
            now = utcnow()
            with self.session_factory() as session:
                for evaluation in evaluations:
                    alert_id = f"alert_budget_{evaluation.budget_id}"
                    row = session.get(OpsAlertEventRow, alert_id)
                    if evaluation.threshold_crossed:
                        severity = "critical" if evaluation.exceeded else "warning"
                        pct = (evaluation.ratio or 0) * 100
                        message = (
                            f"预算超限: budget {evaluation.budget_id} "
                            f"({evaluation.scope_type}:{evaluation.scope_id or '*'}) "
                            f"spend {evaluation.spend.amount} / {evaluation.limit.amount} "
                            f"= {pct:.0f}% of {evaluation.period} limit."
                        )
                        if row is None:
                            session.add(
                                OpsAlertEventRow(
                                    id=alert_id,
                                    code="budget.exceeded",
                                    status="open",
                                    message=message,
                                    severity=severity,
                                    triggered_at=now,
                                )
                            )
                        elif row.status == "resolved":
                            row.status = "open"
                            row.message = message
                            row.severity = severity
                            row.triggered_at = now
                            row.resolved_at = None
                            row.updated_at = now
                        else:
                            row.message = message
                            row.severity = severity
                            row.updated_at = now
                    elif row is not None and row.status != "resolved":
                        row.status = "resolved"
                        row.resolved_at = now
                        row.updated_at = now
                session.commit()
        except Exception:  # pragma: no cover - alert sync must never break dashboard reads
            pass

    def list_alerts(self, *, limit: int = 50) -> list[OpsAlertEvent]:
        with self.session_factory() as session:
            statement = select(OpsAlertEventRow).order_by(OpsAlertEventRow.updated_at.desc()).limit(limit)
            return [alert_row_to_contract(row) for row in session.scalars(statement)]

    def patch_alert_status(self, event_id: str, status: str) -> OpsAlertEvent:
        with self.session_factory() as session:
            row = session.get(OpsAlertEventRow, event_id)
            if row is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Alert not found.")
            row.status = status
            if status == "resolved":
                row.resolved_at = utcnow()
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return alert_row_to_contract(row)

    # ----- §9.2 ops_alert_rules CRUD + §9.8 evaluation engine -----

    def list_alert_rules(self, *, limit: int = 50) -> list[OpsAlertRule]:
        with self.session_factory() as session:
            statement = select(OpsAlertRuleRow).order_by(OpsAlertRuleRow.updated_at.desc()).limit(limit)
            return [alert_rule_row_to_contract(row) for row in session.scalars(statement)]

    def upsert_alert_rule(self, payload: UpsertAlertRuleRequest) -> OpsAlertRule:
        rule = payload.rule
        with self.session_factory() as session:
            row = OpsAlertRuleRow(
                id=rule.id,
                metric=rule.metric,
                condition=rule.condition,
                threshold=rule.threshold,
                scope=rule.scope.model_dump(mode="json"),
                channels=list(rule.channels),
                severity=rule.severity,
                enabled=rule.enabled,
                schema_version=rule.schema_version,
                created_at=rule.created_at,
                updated_at=utcnow(),
            )
            merged = session.merge(row)
            session.commit()
            session.refresh(merged)
            return alert_rule_row_to_contract(merged)

    def patch_alert_rule(self, rule_id: str, payload: PatchAlertRuleRequest) -> OpsAlertRule:
        with self.session_factory() as session:
            row = session.get(OpsAlertRuleRow, rule_id)
            if row is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Alert rule not found.")
            updates = payload.model_dump(exclude_none=True)
            for field in ("threshold", "condition", "severity", "enabled"):
                if field in updates:
                    setattr(row, field, updates[field])
            if "channels" in updates:
                row.channels = list(updates["channels"])
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return alert_rule_row_to_contract(row)

    def _alert_metric_snapshot(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> dict[str, float | None]:
        """The metric values §9.8 rules evaluate against."""

        usage = self.provider_usage(window_start=window_start, window_end=window_end)
        rates = self.yield_funnel(window_start=window_start, window_end=window_end).rates
        cost = self.cost_metrics(window_start=window_start, window_end=window_end)
        provider_success = rates.provider_success_rate if rates else None
        return {
            "yield.true_yield_rate": rates.true_yield_rate if rates else None,
            "yield.qc_fail_rate": (1 - rates.qc_pass_rate) if (rates and rates.qc_pass_rate is not None) else None,
            "provider.failure_rate": (1 - provider_success) if provider_success is not None else None,
            "cost.unpriced": float(usage.unpriced_invocation_count),
            "cost.retry_cost": float(cost.retry_cost.amount),
            "cost.single_video": float(cost.unit_cost_per_finished_video.amount)
            if cost.unit_cost_per_finished_video
            else None,
        }

    # ----- §9.2 failure_taxonomy + §9.6 classification -----

    def record_failure(
        self,
        *,
        target_type: str,
        target_id: str,
        error_code: str | None = None,
        failure_class: str | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
        case_id: str | None = None,
        node_id: str | None = None,
        message: str | None = None,
        dedupe_key: str | None = None,
    ) -> FailureTaxonomyEntry | None:
        """Classify a run/node terminal failure into one of the §9.6 15 classes and
        persist it (idempotent on ``dedupe_key``). Best-effort: a failed write is
        swallowed so failure classification never aborts the originating flow."""

        resolved_class = (
            failure_class
            if failure_class is not None
            else classify_error_code(error_code).value
        )
        try:
            with self.session_factory() as session:
                if dedupe_key is not None:
                    existing = session.scalar(
                        select(FailureTaxonomyRow).where(
                            FailureTaxonomyRow.dedupe_key == dedupe_key
                        )
                    )
                    if existing is not None:
                        return failure_taxonomy_row_to_contract(existing)
                row = FailureTaxonomyRow(
                    id=new_id("failure"),
                    target_type=target_type,
                    target_id=target_id,
                    failure_class=resolved_class,
                    error_code=error_code,
                    run_id=run_id,
                    job_id=job_id,
                    case_id=case_id,
                    node_id=node_id,
                    message=message,
                    dedupe_key=dedupe_key,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                return failure_taxonomy_row_to_contract(row)
        except Exception:  # pragma: no cover - classification must never break a flow
            return None

    def list_failures(
        self,
        *,
        failure_class: str | None = None,
        run_id: str | None = None,
        case_id: str | None = None,
        limit: int = 50,
    ) -> list[FailureTaxonomyEntry]:
        with self.session_factory() as session:
            statement = select(FailureTaxonomyRow).order_by(FailureTaxonomyRow.created_at.desc())
            if failure_class:
                statement = statement.where(FailureTaxonomyRow.failure_class == failure_class)
            if run_id:
                statement = statement.where(FailureTaxonomyRow.run_id == run_id)
            if case_id:
                statement = statement.where(FailureTaxonomyRow.case_id == case_id)
            statement = statement.limit(limit)
            return [failure_taxonomy_row_to_contract(row) for row in session.scalars(statement)]

    def failure_analysis(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> FailureAnalysisReport:
        with self.session_factory() as session:
            statement = select(
                FailureTaxonomyRow.failure_class, func.count(FailureTaxonomyRow.id)
            ).group_by(FailureTaxonomyRow.failure_class)
            if window_start:
                statement = statement.where(FailureTaxonomyRow.created_at >= window_start)
            if window_end:
                statement = statement.where(FailureTaxonomyRow.created_at <= window_end)
            counts = {fc: int(n or 0) for fc, n in session.execute(statement)}
        items = [
            FailureAnalysisItem(failure_class=FailureClass(fc), count=n)
            for fc, n in counts.items()
            if fc in FailureClass._value2member_map_
        ]
        items.sort(key=lambda item: (-item.count, item.failure_class.value))
        return FailureAnalysisReport(
            items=items,
            total=sum(item.count for item in items),
            window_start=window_start,
            window_end=window_end,
        )

    def create_quality_check(
        self, *, target_type: str, target_id: str, payload: CreateQualityCheckRequest
    ) -> ProductionQualityCheck:
        with self.session_factory() as session:
            row = ProductionQualityCheckRow(
                id=new_id("qc"),
                target_type=target_type,
                target_id=target_id,
                check_type=payload.check_type,
                result=payload.result,
                reason_code=payload.reason_code,
                evidence_artifact_id=payload.evidence_artifact_id,
                affects_true_yield=payload.affects_true_yield,
            )
            session.add(row)
            self._record_audit(
                session,
                action="quality_check.created",
                resource_type=target_type,
                resource_id=target_id,
                details={"quality_check_id": row.id, "result": payload.result},
            )
            # §9.5: stage the run-linked qc_* funnel events (persisted best-effort
            # after commit). qc_failed disqualifies the run from true yield; without
            # these the SQL backend never records the disqualifier.
            qc_id = row.id
            event_time = row.created_at or utcnow()
            run_id, job_id, case_id, finished_video_id = self._qc_funnel_linkage(
                session, target_type, target_id
            )
            session.commit()
            session.refresh(row)
        base_event = {
            "job_id": job_id,
            "run_id": run_id,
            "case_id": case_id,
            "finished_video_id": finished_video_id,
            "event_time": event_time,
        }
        funnel_events = [
            {**base_event, "event_type": "qc_started", "dedupe_key": f"{qc_id}:qc_started"}
        ]
        result_value = getattr(payload.result, "value", payload.result)
        terminal = {"passed": "qc_passed", "failed": "qc_failed"}.get(str(result_value))
        if terminal is not None:
            funnel_events.append(
                {**base_event, "event_type": terminal, "dedupe_key": f"{qc_id}:{terminal}"}
            )
        persist_funnel_event_rows(self.session_factory, funnel_events)
        # §9.6: a failed QC is a terminal failure -> classify it as ``qc_failed``.
        if str(result_value) == "failed":
            self.record_failure(
                target_type=target_type,
                target_id=target_id,
                failure_class=FailureClass.qc_failed.value,
                run_id=run_id,
                job_id=job_id,
                case_id=case_id,
                message=payload.reason_code,
                dedupe_key=f"{qc_id}:qc_failed",
            )
        return quality_check_row_to_contract(row)

    def _qc_funnel_linkage(
        self, session: Session, target_type: str, target_id: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Resolve (run_id, job_id, case_id, finished_video_id) for a quality-check
        target so qc_* funnel events stay run-scoped (spec §9.5). A ``run`` target IS
        the run; a ``finished_video`` target resolves through its ``run_id``."""

        if target_type == "run":
            run = session.get(WorkflowRunRow, target_id)
            if run is None:
                return None, None, None, None
            return target_id, run.job_id, run.case_id, None
        finished = session.get(FinishedVideoRow, target_id) if target_id else None
        if finished is None:
            return None, None, None, target_id
        run = session.get(WorkflowRunRow, finished.run_id) if finished.run_id else None
        job_id = run.job_id if run is not None else None
        return finished.run_id, job_id, finished.case_id, target_id

    def decide_approval(
        self, approval_id: str, status: str, payload: ApprovalDecisionRequest
    ) -> ApprovalRequest:
        with self.session_factory() as session:
            row = session.get(ApprovalRequestRow, approval_id)
            if row is None:
                row = ApprovalRequestRow(
                    id=approval_id,
                    resource_type="approval_request",
                    resource_id=approval_id,
                    status=status,
                    reason=payload.reason,
                )
                session.add(row)
            else:
                row.status = status
                row.reason = payload.reason
                row.updated_at = utcnow()
            self._record_audit(
                session,
                action=f"approval.{status}",
                resource_type=row.resource_type,
                resource_id=row.resource_id or approval_id,
                details={"approval_id": approval_id, "reason": payload.reason},
            )
            # §9.5: stage the run-linked manual_* funnel event (persisted best-effort
            # after commit). manual_rejected disqualifies the run from true yield.
            event_time = utcnow()
            run_id, job_id, case_id = self._approval_funnel_linkage(session, row.resource_id)
            session.commit()
            session.refresh(row)
        event_type = "manual_approved" if status == "approved" else "manual_rejected"
        persist_funnel_event_rows(
            self.session_factory,
            [
                {
                    "event_type": event_type,
                    "dedupe_key": f"{approval_id}:{event_type}",
                    "job_id": job_id,
                    "run_id": run_id,
                    "case_id": case_id,
                    "event_time": event_time,
                }
            ],
        )
        # §9.6: a manual rejection is a terminal failure -> classify ``manual_rejected``.
        if status == "rejected":
            self.record_failure(
                target_type="run" if run_id else "approval_request",
                target_id=run_id or approval_id,
                failure_class=FailureClass.manual_rejected.value,
                run_id=run_id,
                job_id=job_id,
                case_id=case_id,
                message=payload.reason,
                dedupe_key=f"{approval_id}:manual_rejected",
            )
        return approval_row_to_contract(row)

    def _approval_funnel_linkage(
        self, session: Session, resource_id: str | None
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve (run_id, job_id, case_id) for an approval decision so manual_*
        funnel events stay run-scoped (spec §9.5). The approval's ``resource_id`` may
        reference a run directly or a finished video that resolves to its run."""

        if not resource_id:
            return None, None, None
        run = session.get(WorkflowRunRow, resource_id)
        if run is not None:
            return run.id, run.job_id, run.case_id
        finished = session.get(FinishedVideoRow, resource_id)
        if finished is None or not finished.run_id:
            return None, None, getattr(finished, "case_id", None)
        run = session.get(WorkflowRunRow, finished.run_id)
        job_id = run.job_id if run is not None else None
        return finished.run_id, job_id, finished.case_id

    def list_audit_events(self, *, limit: int = 50) -> list[AuditEvent]:
        with self.session_factory() as session:
            statement = select(AuditEventRow).order_by(AuditEventRow.created_at.desc()).limit(limit)
            return [audit_row_to_contract(row) for row in session.scalars(statement)]

    def reconcile_billing(self, payload: ReconcileBillingRequest, request_id: str) -> ReconcileBillingResponse:
        reconciliation_run_id = new_id("recon")
        response = self._compute_billing_reconciliation(
            payload,
            reconciliation_run_id=reconciliation_run_id,
            request_id=request_id,
        )
        if payload.dry_run:
            return response
        with self.session_factory() as session:
            session.add(
                ProviderBillingReconciliationRow(
                    id=reconciliation_run_id,
                    provider_id=payload.provider_id,
                    window_start=payload.window_start,
                    window_end=payload.window_end,
                    status=response.status,
                    dry_run=payload.dry_run,
                    estimated_cost=response.estimated_cost.model_dump(mode="json"),
                    recorded_usage_cost=response.recorded_usage_cost.model_dump(mode="json"),
                    variance=response.variance.model_dump(mode="json"),
                    line_items=[
                        item.model_dump(mode="json") for item in response.line_items
                    ],
                    request_id=request_id,
                )
            )
            self._record_audit(
                session,
                action="billing.reconcile_completed",
                resource_type="provider_billing",
                resource_id=reconciliation_run_id,
                details={
                    "reconciliation_run_id": reconciliation_run_id,
                    "provider_id": payload.provider_id,
                    "window_start": payload.window_start.isoformat(),
                    "window_end": payload.window_end.isoformat(),
                    "dry_run": payload.dry_run,
                    "status": response.status,
                    "estimated_cost": response.estimated_cost.model_dump(mode="json"),
                    "recorded_usage_cost": response.recorded_usage_cost.model_dump(mode="json"),
                    "variance": response.variance.model_dump(mode="json"),
                    "line_items": [
                        item.model_dump(mode="json") for item in response.line_items
                    ],
                },
            )
            session.commit()
        return response

    def _compute_billing_reconciliation(
        self,
        payload: ReconcileBillingRequest,
        *,
        reconciliation_run_id: str,
        request_id: str,
    ) -> ReconcileBillingResponse:
        groups: dict[tuple[str, str], dict[str, Decimal | str]] = {}
        with self.session_factory() as session:
            statement = (
                select(ProviderInvocationRow, UsageMeterRecordRow, ProviderPriceItemRow)
                .outerjoin(
                    UsageMeterRecordRow,
                    UsageMeterRecordRow.provider_invocation_id == ProviderInvocationRow.id,
                )
                .outerjoin(
                    ProviderPriceItemRow,
                    ProviderPriceItemRow.id == ProviderInvocationRow.price_item_id,
                )
                .where(ProviderInvocationRow.created_at >= payload.window_start)
                .where(ProviderInvocationRow.created_at <= payload.window_end)
            )
            if payload.provider_id:
                statement = statement.where(
                    ProviderInvocationRow.provider_id == payload.provider_id
                )
            rows = list(session.execute(statement))
            balance_actual_costs = self._balance_actual_costs(session, payload)

        for invocation, usage, price_item in rows:
            provider_id = invocation.provider_id
            capability_id = invocation.capability_id
            key = (provider_id, capability_id)
            group = groups.setdefault(
                key,
                {
                    "estimated": Decimal("0"),
                    "recorded": Decimal("0"),
                    "currency": _money_currency(invocation.estimated_cost),
                },
            )
            group["estimated"] = Decimal(str(group["estimated"])) + _money_amount(
                invocation.estimated_cost
            )
            if invocation.actual_cost is not None:
                recorded = _money_amount(invocation.actual_cost)
            else:
                recorded = self._recorded_usage_cost(usage, price_item)
            group["recorded"] = Decimal(str(group["recorded"])) + recorded

        self._apply_balance_actual_fallback(groups, balance_actual_costs)
        line_items = self._billing_line_items(groups)
        estimated_total = sum(
            (item.estimated_cost.amount for item in line_items),
            Decimal("0"),
        )
        recorded_total = sum(
            (item.recorded_usage_cost.amount for item in line_items),
            Decimal("0"),
        )
        currency = line_items[0].estimated_cost.currency if line_items else "CNY"
        return ReconcileBillingResponse(
            reconciliation_run_id=reconciliation_run_id,
            status="completed",
            estimated_cost=Money(amount=estimated_total, currency=currency),
            recorded_usage_cost=Money(amount=recorded_total, currency=currency),
            variance=Money(amount=recorded_total - estimated_total, currency=currency),
            line_items=line_items,
            request_id=request_id,
        )

    def _recorded_usage_cost(
        self,
        usage: UsageMeterRecordRow | None,
        price_item: ProviderPriceItemRow | None,
    ) -> Decimal:
        if price_item is not None:
            unit_price = _money_amount(price_item.unit_price)
            if price_item.unit == "call":
                return unit_price
            if usage is not None:
                return unit_price * self._usage_quantity(usage, price_item.unit)
        raw_usage_cost = self._raw_usage_cost(usage.raw_usage) if usage is not None else None
        return raw_usage_cost or Decimal("0")

    def _usage_quantity(self, usage: UsageMeterRecordRow, unit: str) -> Decimal:
        if unit == "input_token":
            return Decimal(usage.input_tokens or 0)
        if unit == "output_token":
            return Decimal(usage.output_tokens or 0)
        if unit == "media_second":
            return Decimal(str((usage.audio_seconds or 0) + (usage.video_seconds or 0)))
        return Decimal("0")

    def _raw_usage_cost(self, raw_usage: dict | None) -> Decimal | None:
        if not raw_usage:
            return None
        for key in ("recorded_cost", "actual_cost", "cost", "total_cost"):
            value = raw_usage.get(key)
            if isinstance(value, dict):
                try:
                    return Money.model_validate(value).amount
                except Exception:
                    continue
            if value is not None:
                try:
                    return Decimal(str(value))
                except (TypeError, ValueError):
                    continue
        return None

    def _balance_actual_costs(
        self,
        session: Session,
        payload: ReconcileBillingRequest,
    ) -> dict[str, Decimal]:
        statement = (
            select(ProviderBalanceSnapshotRow)
            .where(ProviderBalanceSnapshotRow.checked_at >= payload.window_start)
            .where(ProviderBalanceSnapshotRow.checked_at <= payload.window_end)
            .order_by(
                ProviderBalanceSnapshotRow.provider_id.asc(),
                ProviderBalanceSnapshotRow.account_group.asc(),
                ProviderBalanceSnapshotRow.checked_at.asc(),
            )
        )
        if payload.provider_id:
            statement = statement.where(ProviderBalanceSnapshotRow.provider_id == payload.provider_id)
        snapshots_by_account: dict[tuple[str, str | None, str], list[ProviderBalanceSnapshotRow]] = {}
        for snapshot in session.scalars(statement):
            if snapshot.balance_amount is None or not snapshot.currency:
                continue
            key = (snapshot.provider_id, snapshot.account_group, snapshot.currency)
            snapshots_by_account.setdefault(key, []).append(snapshot)
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for (provider_id, _account_group, _currency), snapshots in snapshots_by_account.items():
            if len(snapshots) < 2:
                continue
            delta = Decimal(str(snapshots[0].balance_amount)) - Decimal(
                str(snapshots[-1].balance_amount)
            )
            if delta > 0:
                totals[provider_id] += delta
        return totals

    def _apply_balance_actual_fallback(
        self,
        groups: dict[tuple[str, str], dict[str, Decimal | str]],
        balance_actual_costs: dict[str, Decimal],
    ) -> None:
        if not balance_actual_costs:
            return
        keys_by_provider: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for key in groups:
            keys_by_provider[key[0]].append(key)
        for provider_id, actual_cost in balance_actual_costs.items():
            keys = keys_by_provider.get(provider_id, [])
            if not keys:
                groups[(provider_id, "balance.snapshot")] = {
                    "estimated": Decimal("0"),
                    "recorded": actual_cost,
                    "currency": "CNY",
                }
                continue
            provider_recorded = sum(
                (Decimal(str(groups[key]["recorded"])) for key in keys),
                Decimal("0"),
            )
            if provider_recorded == 0 and len(keys) == 1:
                groups[keys[0]]["recorded"] = actual_cost

    def _billing_line_items(
        self,
        groups: dict[tuple[str, str], dict[str, Decimal | str]],
    ) -> list[ReconcileBillingLineItem]:
        items: list[ReconcileBillingLineItem] = []
        for (provider_id, capability_id), group in sorted(groups.items()):
            estimated = Decimal(str(group["estimated"]))
            recorded = Decimal(str(group["recorded"]))
            currency = str(group["currency"] or "CNY")
            items.append(
                ReconcileBillingLineItem(
                    provider_id=provider_id,
                    capability_id=capability_id,
                    estimated_cost=Money(amount=estimated, currency=currency),
                    recorded_usage_cost=Money(amount=recorded, currency=currency),
                    variance=Money(amount=recorded - estimated, currency=currency),
                )
            )
        return items

    def _record_audit(
        self,
        session: Session,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None,
        details: dict,
    ) -> None:
        session.add(
            AuditEventRow(
                id=new_id("audit"),
                actor="system",
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details,
            )
        )
