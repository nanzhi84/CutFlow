from __future__ import annotations

from packages.core.contracts import (
    ApprovalRequest,
    AuditEvent,
    Budget,
    CostRollup,
    FailureTaxonomyEntry,
    Money,
    OpsAlertEvent,
    OpsAlertRule,
    OpsScopeFilter,
    ProductionQualityCheck,
    YieldFunnelEvent,
)
from packages.core.storage.database import (
    ApprovalRequestRow,
    AuditEventRow,
    BudgetRow,
    CostRollupRow,
    FailureTaxonomyRow,
    OpsAlertEventRow,
    OpsAlertRuleRow,
    ProductionQualityCheckRow,
    YieldFunnelEventRow,
)


def cost_rollup_row_to_contract(row: CostRollupRow) -> CostRollup:
    return CostRollup(
        id=row.id,
        group_key=row.group_key,
        group_by=row.group_by,
        estimated_cost=Money.model_validate(row.estimated_cost),
        actual_cost=Money.model_validate(row.actual_cost) if row.actual_cost else None,
        invocations=row.invocations,
        window_start=row.window_start,
        window_end=row.window_end,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def budget_row_to_contract(row: BudgetRow) -> Budget:
    return Budget(
        id=row.id,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        period=row.period or "day",
        limit=Money.model_validate(row.limit),
        alert_threshold=row.alert_threshold,
        enabled=row.enabled,
        enforce=bool(getattr(row, "enforce", False)),
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def alert_rule_row_to_contract(row: OpsAlertRuleRow) -> OpsAlertRule:
    return OpsAlertRule(
        id=row.id,
        metric=row.metric,
        condition=row.condition,
        threshold=row.threshold,
        scope=OpsScopeFilter.model_validate(row.scope or {}),
        channels=list(row.channels or []),
        severity=row.severity,
        enabled=row.enabled,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def failure_taxonomy_row_to_contract(row: FailureTaxonomyRow) -> FailureTaxonomyEntry:
    return FailureTaxonomyEntry(
        id=row.id,
        target_type=row.target_type,
        target_id=row.target_id,
        failure_class=row.failure_class,
        error_code=row.error_code,
        run_id=row.run_id,
        job_id=row.job_id,
        case_id=row.case_id,
        node_id=row.node_id,
        message=row.message,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def alert_row_to_contract(row: OpsAlertEventRow) -> OpsAlertEvent:
    return OpsAlertEvent(
        id=row.id,
        code=row.code,
        rule_id=row.rule_id,
        status=row.status,
        message=row.message,
        severity=row.severity,
        triggered_at=row.triggered_at,
        resolved_at=row.resolved_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def yield_event_row_to_contract(row: YieldFunnelEventRow) -> YieldFunnelEvent:
    return YieldFunnelEvent(
        id=row.id,
        job_id=row.job_id,
        run_id=row.run_id,
        finished_video_id=row.finished_video_id,
        publish_package_id=row.publish_package_id,
        publish_attempt_id=row.publish_attempt_id,
        event_type=row.event_type,
        event_time=row.event_time,
        dedupe_key=row.dedupe_key,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def quality_check_row_to_contract(row: ProductionQualityCheckRow) -> ProductionQualityCheck:
    return ProductionQualityCheck(
        id=row.id,
        target_type=row.target_type,
        target_id=row.target_id,
        check_type=row.check_type,
        result=row.result,
        reason_code=row.reason_code,
        evidence_artifact_id=row.evidence_artifact_id,
        affects_true_yield=row.affects_true_yield,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def approval_row_to_contract(row: ApprovalRequestRow) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        resource_type=row.resource_type,
        resource_id=row.resource_id or row.id,
        status=row.status,
        reason=row.reason,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def audit_row_to_contract(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        id=row.id,
        actor=row.actor,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        details=row.details,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
