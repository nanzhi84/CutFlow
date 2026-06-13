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


class CostRollup(EntityMeta):
    group_key: str
    group_by: str | None = None
    estimated_cost: Money
    actual_cost: Money | None = None
    invocations: int = 0


class CostRollupQuery(OpsDashboardQuery):
    group_by: Literal["case", "provider", "model", "prompt_version", "run", "job"] | None = None


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


class YieldFunnelResponse(ContractModel):
    events: list[YieldFunnelEvent]
    true_yield_rate: float | None = None


class Budget(EntityMeta):
    scope_type: str
    scope_id: str | None = None
    limit: Money
    alert_threshold: float = Field(0.8, ge=0, le=1)
    enabled: bool = True


class BudgetQuery(BaseListQuery):
    scope_type: str | None = None


class UpsertBudgetRequest(ContractModel):
    budget: Budget


class PatchBudgetRequest(ContractModel):
    limit: Money | None = None
    alert_threshold: float | None = None
    enabled: bool | None = None


class OpsAlertEvent(EntityMeta):
    code: str
    status: Literal["open", "acknowledged", "resolved"] = "open"
    message: str
    severity: Literal["info", "warning", "error"] = "warning"


class AcknowledgeAlertRequest(ContractModel):
    note: str | None = None


class ResolveAlertRequest(ContractModel):
    resolution: str


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
