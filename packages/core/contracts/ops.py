"""Ops domain: dashboards, cost rollups, yield funnel, budgets, alerts, quality, audit, and imports."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import Field, JsonValue

from .base import BaseListQuery, ContractModel, EntityMeta, Money, NodeError, utcnow
from .providers import ProviderUsageReport


class OpsDashboardQuery(ContractModel):
    window_start: datetime
    window_end: datetime


# §9.2 / §26.1: the dimensions a cost rollup may GROUP BY.
CostGroupBy = Literal["case", "provider", "model", "prompt_version", "run", "job"]


class CostRollup(EntityMeta):
    group_key: str
    group_by: str | None = None
    estimated_cost: Money
    actual_cost: Money | None = None
    invocations: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None


class CostRollupQuery(OpsDashboardQuery):
    group_by: CostGroupBy | None = None


class CostMetrics(ContractModel):
    """§9.4 / §26.2 cost indicators for an ops window.

    All monetary values are CNY ``Money``. The ``unit_cost_per_*`` ratios divide
    total provider-invocation cost by the relevant §9.5 funnel count and are
    ``None`` when that denominator is 0 (no finished/qc-passed/published videos).
    ``cost_variance = actual_cost - estimated_cost`` is ``None`` until actual_cost
    is backfilled. ``provider_cost`` / ``model_cost`` / ``prompt_version_cost`` map
    a dimension key to its cost share."""

    window_start: datetime | None = None
    window_end: datetime | None = None
    estimated_cost: Money
    actual_cost: Money | None = None
    cost_variance: Money | None = None
    wasted_cost: Money
    retry_cost: Money
    finished_video_count: int = 0
    qc_passed_count: int = 0
    published_count: int = 0
    unit_cost_per_finished_video: Money | None = None
    unit_cost_per_qc_passed_video: Money | None = None
    unit_cost_per_published_video: Money | None = None
    provider_cost: dict[str, Money] = Field(default_factory=dict)
    model_cost: dict[str, Money] = Field(default_factory=dict)
    prompt_version_cost: dict[str, Money] = Field(default_factory=dict)


class YieldFunnelEvent(EntityMeta):
    job_id: str | None = None
    run_id: str | None = None
    finished_video_id: str | None = None
    publish_package_id: str | None = None
    publish_attempt_id: str | None = None
    event_type: str
    event_time: datetime
    dedupe_key: str


class YieldFunnelQuery(OpsDashboardQuery):
    case_id: str | None = None


class YieldRates(ContractModel):
    """§9.5 / §26.3 成品率指标. Each rate is a fraction in [0, 1] or ``None`` when
    its denominator is 0 (no data). Denominators follow §26.3 exactly:
    technical_success uses started runs, finished_video / true_yield use submitted
    jobs, qc_pass / rework / discard use finished videos, approval_pass uses manual
    reviews started, publish_success uses publish_started packages, stage_pass uses
    node_started. ``prompt_version_yield`` maps prompt_version_id -> its true-yield
    fraction over the runs that used it."""

    technical_success_rate: float | None = None
    finished_video_rate: float | None = None
    qc_pass_rate: float | None = None
    approval_pass_rate: float | None = None
    publish_success_rate: float | None = None
    true_yield_rate: float | None = None
    rework_rate: float | None = None
    discard_rate: float | None = None
    stage_pass_rate: float | None = None
    provider_success_rate: float | None = None
    prompt_version_yield: dict[str, float] = Field(default_factory=dict)


class YieldFunnelResponse(ContractModel):
    events: list[YieldFunnelEvent]
    true_yield_rate: float | None = None
    rates: YieldRates | None = None


BudgetPeriod = Literal["day", "week", "month"]


class Budget(EntityMeta):
    scope_type: str
    scope_id: str | None = None
    period: BudgetPeriod = "day"
    limit: Money
    alert_threshold: float = Field(0.8, ge=0, le=1)
    enabled: bool = True
    enforce: bool = False


class BudgetQuery(BaseListQuery):
    scope_type: str | None = None


class UpsertBudgetRequest(ContractModel):
    budget: Budget


class PatchBudgetRequest(ContractModel):
    limit: Money | None = None
    alert_threshold: float | None = None
    enabled: bool | None = None
    enforce: bool | None = None
    period: BudgetPeriod | None = None


class BudgetEvaluation(ContractModel):
    """§9.8 预算执行: current-period spend evaluated against a Budget.

    ``ratio`` = spend / limit (``None`` if the limit is 0). ``exceeded`` flags a
    hard over-limit; ``threshold_crossed`` flags ``ratio >= alert_threshold`` (the
    75/90%-style warning level the OLD quota_monitor produced)."""

    budget_id: str
    scope_type: str
    scope_id: str | None = None
    period: BudgetPeriod
    period_start: datetime
    spend: Money
    limit: Money
    ratio: float | None = None
    threshold_crossed: bool = False
    exceeded: bool = False


class OpsAlertEvent(EntityMeta):
    code: str
    rule_id: str | None = None
    status: Literal["open", "acknowledged", "resolved"] = "open"
    message: str
    severity: Literal["info", "warning", "error", "critical"] = "warning"
    triggered_at: datetime | None = None
    resolved_at: datetime | None = None


class AcknowledgeAlertRequest(ContractModel):
    note: str | None = None


class ResolveAlertRequest(ContractModel):
    resolution: str


class OpsScopeFilter(ContractModel):
    """§26.1 OpsScopeFilter — narrows an alert rule to a subset of the pipeline."""

    case_ids: list[str] = Field(default_factory=list)
    provider_ids: list[str] = Field(default_factory=list)
    model_ids: list[str] = Field(default_factory=list)
    capability_id: str | None = None
    prompt_template_ids: list[str] = Field(default_factory=list)
    prompt_version_ids: list[str] = Field(default_factory=list)
    environment: Literal["local", "dev", "staging", "prod"] | None = None


class OpsAlertRule(EntityMeta):
    """§9.2 ops_alert_rules / §26.1 OpsAlertRule — a metric threshold that the
    alert engine periodically evaluates and emits OpsAlertEvent for."""

    metric: str
    condition: Literal["gt", "gte", "lt", "lte", "change_gt"]
    threshold: float
    scope: OpsScopeFilter = Field(default_factory=OpsScopeFilter)
    channels: list[str] = Field(default_factory=list)
    severity: Literal["info", "warning", "error", "critical"] = "warning"
    enabled: bool = True


class OpsAlertRuleQuery(BaseListQuery):
    metric: str | None = None
    enabled: bool | None = None


class UpsertAlertRuleRequest(ContractModel):
    rule: OpsAlertRule


class PatchAlertRuleRequest(ContractModel):
    threshold: float | None = None
    condition: Literal["gt", "gte", "lt", "lte", "change_gt"] | None = None
    severity: Literal["info", "warning", "error", "critical"] | None = None
    channels: list[str] | None = None
    enabled: bool | None = None


class FailureClass(str, Enum):
    """§9.6 失败分类法 — the 15 required failure classes."""

    provider_error = "provider_error"
    provider_timeout = "provider_timeout"
    quota_exceeded = "quota_exceeded"
    price_missing = "price_missing"
    prompt_render_error = "prompt_render_error"
    prompt_output_invalid = "prompt_output_invalid"
    material_insufficient = "material_insufficient"
    timeline_invalid = "timeline_invalid"
    render_failed = "render_failed"
    subtitle_failed = "subtitle_failed"
    bgm_failed = "bgm_failed"
    lipsync_quality_failed = "lipsync_quality_failed"
    qc_failed = "qc_failed"
    publish_failed = "publish_failed"
    manual_rejected = "manual_rejected"


class FailureTaxonomyEntry(EntityMeta):
    """§9.2 failure_taxonomy — one classified run/node terminal failure."""

    target_type: Literal[
        "run", "node_run", "finished_video", "publish_attempt", "approval_request"
    ]
    target_id: str
    failure_class: FailureClass
    error_code: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    case_id: str | None = None
    node_id: str | None = None
    message: str | None = None


class FailureTaxonomyQuery(BaseListQuery):
    failure_class: FailureClass | None = None
    run_id: str | None = None
    case_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None


class FailureAnalysisItem(ContractModel):
    failure_class: FailureClass
    count: int


class FailureAnalysisReport(ContractModel):
    items: list[FailureAnalysisItem] = Field(default_factory=list)
    total: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None


class ProductionQualityCheck(EntityMeta):
    target_type: Literal["run", "finished_video"]
    target_id: str
    check_type: Literal["auto", "manual", "platform_feedback"] = "manual"
    result: Literal["passed", "failed", "warning", "manual_required"]
    reason_code: str | None = None
    evidence_artifact_id: str | None = None
    affects_true_yield: bool = True


class CreateQualityCheckRequest(ContractModel):
    check_type: Literal["auto", "manual", "platform_feedback"] = "manual"
    result: Literal["passed", "failed", "warning", "manual_required"]
    reason_code: str | None = None
    evidence_artifact_id: str | None = None
    affects_true_yield: bool = True


class ApprovalRequest(EntityMeta):
    resource_type: str
    resource_id: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    reason: str | None = None


class ApprovalDecisionRequest(ContractModel):
    reason: str


class AuditEvent(EntityMeta):
    actor: str | None = None
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class AuditEventQuery(BaseListQuery):
    actor: str | None = None
    resource_type: str | None = None
    action: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None


class OpsDashboardVm(ContractModel):
    usage: ProviderUsageReport
    yield_funnel: YieldFunnelResponse
    alerts: list[OpsAlertEvent]
    cost_rollups: list[CostRollup]
    cost_metrics: CostMetrics | None = None
    yield_rates: YieldRates | None = None
    budget_evaluations: list[BudgetEvaluation] = Field(default_factory=list)
    failure_analysis: FailureAnalysisReport | None = None


class ImportBatchStatus(str, Enum):
    created = "created"
    running = "running"
    completed = "completed"
    failed = "failed"
    partially_failed = "partially_failed"


class CreateImportBatchRequest(ContractModel):
    import_type: Literal[
        "case",
        "script",
        "media",
        "finished_video",
        "video_version",
        "publish_record",
        "performance",
        "prompt_seed",
        "provider_price",
    ]
    rows_artifact_id: str | None = None
    rows: list[JsonValue] | None = None
    dry_run: bool = False
    idempotency_key: str | None = None


class ImportRowResult(ContractModel):
    row_index: int
    status: Literal["created", "skipped", "failed"]
    external_id: str | None = None
    internal_id: str | None = None
    error: NodeError | None = None


class ImportBatchReport(ContractModel):
    batch_id: str
    import_type: str
    status: ImportBatchStatus
    created_count: int
    skipped_count: int
    failed_count: int
    results: list[ImportRowResult]
    mapping_artifact_id: str | None = None
    request_id: str


class OutboxEvent(EntityMeta):
    topic: str
    aggregate_type: str
    aggregate_id: str
    dedupe_key: str
    payload_schema: str
    payload: JsonValue
    status: Literal["pending", "published", "failed"] = "pending"
    attempts: int = 0
    available_at: datetime = Field(default_factory=utcnow)
    published_at: datetime | None = None
    last_error: str | None = None
