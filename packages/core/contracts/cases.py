"""Cases domain: case metadata, knowledge/memory, scripts, performance, and the case agent."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import Field, JsonValue

from .base import BaseListQuery, ContractModel, EntityMeta, RunStatus, utcnow


class CaseListQuery(BaseListQuery):
    search: str | None = None
    owner_user_id: str | None = None


class CreateCaseRequest(ContractModel):
    name: str
    description: str | None = None
    industry: str | None = None
    product: str | None = None
    target_audience: str | None = None


class PatchCaseRequest(ContractModel):
    name: str | None = None
    description: str | None = None
    product: str | None = None
    target_audience: str | None = None
    status: Literal["active", "archived"] | None = None


class DeleteCaseRequest(ContractModel):
    reason: str | None = None


class CaseListItem(EntityMeta):
    name: str
    owner_user_id: str | None = None
    active_memory_count: int = 0
    status: Literal["active", "archived"] = "active"


class CaseDetail(CaseListItem):
    description: str | None = None
    industry: str | None = None
    product: str | None = None
    target_audience: str | None = None


class ScriptVersion(EntityMeta):
    case_id: str
    title: str
    script: str
    creative_intent_artifact_id: str | None = None
    adopted_from_draft_id: str | None = None


class VideoVersion(EntityMeta):
    case_id: str
    script_version_id: str | None = None
    finished_video_id: str | None = None
    timeline_plan_artifact_id: str
    style_plan_artifact_id: str


class PublishRecord(EntityMeta):
    case_id: str
    video_version_id: str | None = None
    publish_package_id: str | None = None
    publish_batch_id: str | None = None
    platform: str
    status: Literal["draft", "submitted", "published", "failed"] = "draft"
    cover_artifact_id: str | None = None
    published_at: datetime | None = None


class PerformanceObservation(EntityMeta):
    case_id: str
    publish_record_id: str
    metric_name: str
    metric_value: float
    observed_at: datetime = Field(default_factory=utcnow)


class PerformanceMetricView(ContractModel):
    impressions: int = 0
    clicks: int = 0
    views: int = 0
    likes: int = 0
    conversion_rate: float | None = None


class CreativeFeatureVector(ContractModel):
    hook_type: str | None = None
    duration_sec: float | None = None
    broll_count: int = 0
    title_tokens: int = 0


class CaseMemoryScope(ContractModel):
    channel: str | None = None
    audience: str | None = None
    product: str | None = None


class CaseMemory(EntityMeta):
    case_id: str
    status: Literal["proposed", "approved", "active", "deprecated", "rejected", "superseded"] = "proposed"
    scope: CaseMemoryScope = Field(default_factory=CaseMemoryScope)
    insight: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0, le=1)


class MemoryProposal(CaseMemory):
    proposed_by_reflection_run_id: str | None = None


class ReflectionRun(EntityMeta):
    case_id: str
    status: RunStatus = RunStatus.created
    window: Literal["24h", "3d", "7d", "30d"] = "7d"
    report_artifact_id: str | None = None


class CaseAgentSourceBinding(EntityMeta):
    case_id: str
    source_type: Literal["url", "text", "file", "manual_note"]
    source_ref: str
    title: str | None = None


class CreativeBrief(EntityMeta):
    case_id: str
    summary: str
    source_binding_ids: list[str] = Field(default_factory=list)


class ScriptDraft(EntityMeta):
    case_id: str
    title: str
    script: str
    status: Literal["draft", "adopted", "rejected"] = "draft"
    memory_ids: list[str] = Field(default_factory=list)


class CaseAgentRun(EntityMeta):
    case_id: str
    goal: Literal["brief", "script_draft", "memory_proposal"]
    status: RunStatus = RunStatus.created
    source_binding_ids: list[str] = Field(default_factory=list)


class CaseAgentRunQuery(BaseListQuery):
    status: str | None = None


class CreateSourceBindingRequest(ContractModel):
    source_type: Literal["url", "text", "file", "manual_note"]
    source_ref: str
    title: str | None = None


class ImportCaseSourceRequest(ContractModel):
    source_binding_id: str
    provider_profile_id: str | None = None


class StartCaseAgentRunRequest(ContractModel):
    goal: Literal["brief", "script_draft", "memory_proposal"]
    source_binding_ids: list[str] = Field(default_factory=list)


class CaseAgentRunDetail(ContractModel):
    run: CaseAgentRun
    briefs: list[CreativeBrief] = Field(default_factory=list)
    drafts: list[ScriptDraft] = Field(default_factory=list)
    memory_proposals: list[MemoryProposal] = Field(default_factory=list)


class ScriptDraftQuery(BaseListQuery):
    status: str | None = None


class AdoptScriptDraftRequest(ContractModel):
    title: str | None = None
    publish_content: str | None = None


class MemoryProposalQuery(BaseListQuery):
    status: str | None = None


class ApproveMemoryRequest(ContractModel):
    reason: str | None = None


class RejectMemoryRequest(ContractModel):
    reason: str


class CaseKnowledgeResponse(ContractModel):
    case_id: str
    memories: list[CaseMemory]
    recent_script_versions: list[ScriptVersion]
    recent_video_versions: list[VideoVersion]


class CasePerformanceQuery(ContractModel):
    window: Literal["24h", "3d", "7d", "30d"] = "7d"


class CasePerformanceResponse(ContractModel):
    metrics: PerformanceMetricView
    observations: list[PerformanceObservation]


class StartReflectionRunRequest(ContractModel):
    window: Literal["24h", "3d", "7d", "30d"] = "7d"
    force: bool = False


class GenerateScriptWithMemoryRequest(ContractModel):
    brief: str
    memory_ids: list[str] = Field(default_factory=list)


class ReferenceExtractRequest(ContractModel):
    url: str = Field(min_length=1)
    language: str = "zh"


class ReferenceExtractResult(ContractModel):
    reference_script: str
    source: Literal["subtitle", "asr"]
    title: str | None = None
    platform: str
    duration_sec: float | None = None
    resolved_url: str


class PerformanceAttributionResponse(ContractModel):
    video_version_id: str
    feature_vector: CreativeFeatureVector | None = None
    observations: list[PerformanceObservation]
    contributing_memories: list[CaseMemory] = Field(default_factory=list)


class CreativePattern(EntityMeta):
    case_id: str
    label: str
    lift: float | None = None
    evidence_count: int = 0


class CaseInsightCard(EntityMeta):
    case_id: str
    title: str
    body: str
    severity: Literal["info", "warning", "success"] = "info"


class MetricsImportRequest(ContractModel):
    rows: list[dict[str, JsonValue]]
    dry_run: bool = False
