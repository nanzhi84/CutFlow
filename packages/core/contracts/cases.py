"""Cases domain: case metadata, knowledge/memory, scripts, performance, and the case agent."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import Field, JsonValue

from .base import BaseListQuery, ContractModel, EntityMeta, RunStatus, utcnow


class CaseListQuery(BaseListQuery):
    search: str | None = None
    owner_user_id: str | None = None
    industry: str | None = None


class CreateCaseRequest(ContractModel):
    name: str
    description: str | None = None
    industry: str | None = None
    product: str | None = None
    target_audience: str | None = None
    key_selling_points: list[str] = Field(default_factory=list)
    ip_persona: str | None = None
    brand_voice: str | None = None
    strategy_tags: list[str] = Field(default_factory=list)
    brand_keywords: list[str] = Field(default_factory=list)
    competitor_names: list[str] = Field(default_factory=list)


class PatchCaseRequest(ContractModel):
    name: str | None = None
    description: str | None = None
    product: str | None = None
    target_audience: str | None = None
    status: Literal["active", "archived"] | None = None
    industry: str | None = None
    key_selling_points: list[str] | None = None
    ip_persona: str | None = None
    brand_voice: str | None = None
    strategy_tags: list[str] | None = None
    brand_keywords: list[str] | None = None
    competitor_names: list[str] | None = None


class DeleteCaseRequest(ContractModel):
    reason: str | None = None


class CaseListItem(EntityMeta):
    name: str
    owner_user_id: str | None = None
    active_memory_count: int = 0
    status: Literal["active", "archived"] = "active"
    industry: str | None = None
    material_count: int = 0
    script_count: int = 0
    voice_count: int = 0
    quality_count: int = 0


class CaseDetail(CaseListItem):
    description: str | None = None
    product: str | None = None
    target_audience: str | None = None
    key_selling_points: list[str] = Field(default_factory=list)
    ip_persona: str | None = None
    brand_voice: str | None = None
    strategy_tags: list[str] = Field(default_factory=list)
    brand_keywords: list[str] = Field(default_factory=list)
    competitor_names: list[str] = Field(default_factory=list)


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


MetricWindow = Literal["1h", "24h", "3d", "7d", "30d"]


class PerformanceObservation(EntityMeta):
    case_id: str
    publish_record_id: str
    # §25.1 / §8.3: an observation must be able to bind back to the video lineage
    # and carry the platform/account/window dimensions used for grouping & scoring.
    video_version_id: str | None = None
    platform: str | None = None
    account_id: str | None = None
    window: MetricWindow | None = None
    # Generic single-metric shape (kept for backward compatibility / manual rows).
    metric_name: str
    metric_value: float
    # §8.3 canonical metrics (optional; populated by structured imports).
    impressions: int | None = None
    views: int | None = None
    avg_watch_sec: float | None = None
    completion_rate: float | None = None
    like_rate: float | None = None
    comment_rate: float | None = None
    share_rate: float | None = None
    follow_rate: float | None = None
    conversion_count: int | None = None
    conversion_rate: float | None = None
    raw_metrics: dict[str, JsonValue] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utcnow)


class PerformanceMetricView(ContractModel):
    impressions: int = 0
    clicks: int = 0
    views: int = 0
    likes: int = 0
    conversion_rate: float | None = None


class PerformanceScore(EntityMeta):
    """§25.6 normalized, windowed, confidence-gated performance score.

    A score never treats raw views/impressions as quality directly: when the
    observation's impression/view volume is below ``MIN_CONFIDENT_IMPRESSIONS``
    (or the window is only an early 24h signal) the score is emitted with a
    reduced ``confidence`` and an ``excluded_reason`` so callers (memory
    activation, high/low-performance recall) can refuse to draw conclusions.
    """

    observation_id: str
    case_id: str
    video_version_id: str | None = None
    platform: str | None = None
    account_id: str | None = None
    window: MetricWindow = "7d"
    primary_metric: Literal[
        "completion_rate", "follow_rate", "conversion_rate", "engagement_rate"
    ] = "engagement_rate"
    normalized_score: float = Field(0.0, ge=0, le=1)
    confidence: float = Field(0.0, ge=0, le=1)
    sample_size: int = 0
    excluded_reason: str | None = None


class CreativeFeatureVector(EntityMeta):
    case_id: str = ""
    script_version_id: str | None = None
    video_version_id: str | None = None
    hook_type: str | None = None
    script_structure: str | None = None
    topic_tags: list[str] = Field(default_factory=list)
    cta_type: str | None = None
    angle: str | None = None
    duration_sec: float | None = None
    broll_density: float | None = None
    cut_density: float | None = None
    subtitle_style_id: str | None = None
    bgm_id: str | None = None
    cover_style: str | None = None
    material_ids: list[str] = Field(default_factory=list)
    # Legacy convenience counters retained for existing callers/UI.
    broll_count: int = 0
    title_tokens: int = 0


class CaseMemoryScope(ContractModel):
    # Legacy single-value dimensions (kept; still emitted/consumed by older UI).
    channel: str | None = None
    audience: str | None = None
    product: str | None = None
    # §8.3 scope dimensions for recall filtering.
    scope_key: str | None = None
    applies_to_case_ids: list[str] = Field(default_factory=list)
    applies_to_platforms: list[str] = Field(default_factory=list)
    applies_to_audience_segments: list[str] = Field(default_factory=list)
    applies_to_script_intents: list[str] = Field(default_factory=list)
    excluded_case_ids: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None


MemoryType = Literal[
    "script_pattern", "video_pattern", "audience_insight", "editing_rule", "negative_lesson"
]


class CaseMemory(EntityMeta):
    case_id: str
    status: Literal["proposed", "approved", "active", "deprecated", "rejected", "superseded"] = "proposed"
    memory_type: MemoryType = "script_pattern"
    scope: CaseMemoryScope = Field(default_factory=CaseMemoryScope)
    insight: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0, le=1)
    sample_size: int = 0
    supersedes_memory_id: str | None = None


class MemoryProposal(CaseMemory):
    proposed_by_reflection_run_id: str | None = None


MemoryRecallMode = Literal[
    "recent", "topic", "platform", "memory_type", "high_performance", "low_performance"
]


class MemoryRecallQuery(ContractModel):
    """§25.8 LoadCaseContextNode retrieval modes for recalling case memories."""

    mode: MemoryRecallMode = "recent"
    topic: str | None = None
    platform: str | None = None
    memory_type: MemoryType | None = None
    scope_key: str | None = None
    limit: int = Field(20, ge=1, le=200)


class CaseKnowledgeItem(EntityMeta):
    """§25.8 unified knowledge index row spanning script/video/publish/metric/memory."""

    case_id: str
    kind: Literal["script", "video", "publish", "metric", "reflection", "memory"]
    ref_id: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    embedding_ref: str | None = None
    score: float | None = None


class MemoryRecallResponse(ContractModel):
    case_id: str
    mode: MemoryRecallMode
    memories: list[CaseMemory] = Field(default_factory=list)


class ReflectionRun(EntityMeta):
    case_id: str
    status: RunStatus = RunStatus.created
    window: Literal["24h", "3d", "7d", "30d"] = "7d"
    report_artifact_id: str | None = None
    # §8.3: lineage of what the reflection actually read and produced.
    input_observation_ids: list[str] = Field(default_factory=list)
    input_feature_vector_ids: list[str] = Field(default_factory=list)
    memory_proposal_ids: list[str] = Field(default_factory=list)
    sample_size: int = 0


class CaseAgentSourceBinding(EntityMeta):
    case_id: str
    source_type: Literal["url", "text", "file", "manual_note"]
    source_ref: str
    title: str | None = None


class CreativeBrief(EntityMeta):
    case_id: str
    summary: str
    source_binding_ids: list[str] = Field(default_factory=list)
    topic: str | None = None
    audience: str | None = None
    key_insights: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    generated_by_run_id: str | None = None


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
    scores: list[PerformanceScore] = Field(default_factory=list)


class StartReflectionRunRequest(ContractModel):
    window: Literal["24h", "3d", "7d", "30d"] = "7d"
    force: bool = False


class GenerateScriptWithMemoryRequest(ContractModel):
    brief: str
    memory_ids: list[str] = Field(default_factory=list)
    persona_mode: Literal["hard_ad", "ip_persona"] = "hard_ad"
    operation: Literal["polish", "fresh", "remix", "clone", "generate", "semantic"] = "generate"
    strategy_tags: list[str] = Field(default_factory=list)
    reference_script: str | None = None
    duration: str | None = None


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


class ReferenceCookieImportRequest(ContractModel):
    cookie_text: str = Field(min_length=1)
    format: Literal["auto", "header", "netscape", "json"] = "auto"
    source: str | None = None


class ReferenceCookieStatus(ContractModel):
    cookie_present: bool
    cookie_count: int = 0
    earliest_expiry: datetime | None = None
    expired: bool = False
    updated_at: datetime | None = None
    source: str | None = None


class ReferenceCookieImportResponse(ContractModel):
    success: bool
    message: str
    status: ReferenceCookieStatus
    request_id: str


class ReferenceCookieTestRequest(ContractModel):
    url: str | None = None


class ReferenceCookieTestResponse(ContractModel):
    success: bool
    message: str
    test_url: str | None = None
    title: str | None = None
    status: ReferenceCookieStatus
    request_id: str


class ReferenceExtractorStatusResponse(ContractModel):
    cookie: ReferenceCookieStatus
    chrome_available: bool = False
    chrome_path: str | None = None
    playwright_available: bool = False
    auto_refresh_supported: bool = False
    request_id: str


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


MetricsMatchingPolicy = Literal[
    "external_post_id", "platform_item_id", "published_url", "strict_manual"
]


class MetricsImportRequest(ContractModel):
    """§25.4 metrics import request.

    ``matching_policy`` selects the deterministic key used to resolve each row's
    ``publish_record_id``. Title + publish-time guessing is forbidden unless the
    policy is ``strict_manual`` (which also writes a warning into the report).
    """

    rows: list[dict[str, JsonValue]]
    dry_run: bool = False
    source: Literal["manual_csv", "oceanengine_rpa", "platform_api"] = "manual_csv"
    platform: str | None = None
    account_id: str | None = None
    matching_policy: MetricsMatchingPolicy = "external_post_id"


class MetricsImportResponse(ContractModel):
    """§25.4 metrics import response with matched/unmatched accounting."""

    imported_count: int = 0
    unmatched_count: int = 0
    unmatched_rows_artifact_id: str | None = None
    observation_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    request_id: str


OceanEngineSourcePage = Literal[
    "video_analysis",
    "localpush_account",
    "localpush_unit",
    "comment_content",
]


class OceanEngineMetricRow(ContractModel):
    """A single normalized OceanEngine (巨量) offline-import metric/comment record.

    ``source_page`` identifies the RPA export the row came from. ``external_ref``
    is the most stable identifier the export carries (material/video/unit id)
    used for downstream matching. ``metrics`` holds the numeric measures keyed by
    canonical metric name; ``attributes`` keeps non-numeric context. ``raw`` is the
    untouched source row, and ``row_fingerprint`` is a content hash for dedupe.
    """

    source_page: OceanEngineSourcePage
    external_ref: str | None = None
    title: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    attributes: dict[str, str] = Field(default_factory=dict)
    raw: dict[str, str] = Field(default_factory=dict)
    row_fingerprint: str
