"""Cases domain: case metadata, scripts, performance, and case rubric learning."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import Field, JsonValue

from .base import BaseListQuery, ContractModel, EntityMeta, utcnow


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
    channel: str | None = None
    audience: str | None = None
    product: str | None = None
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
    status: Literal["active", "deprecated", "superseded"] = "active"
    memory_type: MemoryType = "script_pattern"
    scope: CaseMemoryScope = Field(default_factory=CaseMemoryScope)
    insight: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0, le=1)
    sample_size: int = 0
    supersedes_memory_id: str | None = None


class ScriptDraft(EntityMeta):
    case_id: str
    title: str
    script: str
    status: Literal["draft", "adopted", "rejected"] = "draft"
    memory_ids: list[str] = Field(default_factory=list)


class ScriptDraftQuery(BaseListQuery):
    status: str | None = None


class AdoptScriptDraftRequest(ContractModel):
    title: str | None = None
    publish_content: str | None = None


class CasePerformanceQuery(ContractModel):
    window: Literal["24h", "3d", "7d", "30d"] = "7d"


class CasePerformanceResponse(ContractModel):
    metrics: PerformanceMetricView
    observations: list[PerformanceObservation]
    scores: list[PerformanceScore] = Field(default_factory=list)


class GenerateScriptWithMemoryRequest(ContractModel):
    brief: str
    memory_ids: list[str] = Field(default_factory=list)
    persona_mode: Literal["hard_ad", "ip_persona"] = "hard_ad"
    operation: Literal["polish", "fresh", "remix", "clone", "generate", "semantic"] = "generate"
    strategy_tags: list[str] = Field(default_factory=list)
    reference_script: str | None = None
    duration: str | None = None
    # >1 时生成多版草稿、各自盲打分（§6.2）；默认 1。注意 openapi-typescript 会把带默认值的
    # 标量字段在 schema.d.ts 标为 required（同 persona_mode/operation），故前端调用须显式传。
    variation_count: int = Field(1, ge=1, le=5)


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


# ---------------------------------------------------------------------------
# 评分卡自进化（case_rubric_v1）
# ---------------------------------------------------------------------------

RubricDimensionKind = Literal["categorical", "numeric"]


class RubricDimension(ContractModel):
    """评分卡的一个维度：对齐 CreativeFeatureVector 字段，带权重与取值→分映射。"""

    key: str  # 对齐 CreativeFeatureVector 字段名，如 "hook_type" / "cut_density"
    label: str  # 人话维度名，如 "开场强度"
    weight: float = Field(0.0, ge=0, le=1)
    kind: RubricDimensionKind = "categorical"
    # categorical: 取值 → [0,1] 评分表；numeric: [low, high] 线性归一。
    value_scores: dict[str, float] = Field(default_factory=dict)
    numeric_low: float | None = None
    numeric_high: float | None = None


class CaseRubric(EntityMeta):
    """案例评分卡：一个案例"什么样的内容更可能成"的可执行打分公式（§6）。"""

    case_id: str
    version: int = 1
    status: Literal["draft", "active", "superseded"] = "active"
    dimensions: list[RubricDimension] = Field(default_factory=list)
    fitted_from_sample_size: int = 0
    cold_start: bool = True
    supersedes_version: int | None = None


ScoreBand = Literal["top", "ok", "low"]


class ScorePrediction(EntityMeta):
    """对一版脚本的盲预测（§6.2）：``locked_at`` 之后 composite/维度分不可改；
    任何 ``performance_scored`` 结算必须晚于 ``locked_at``。"""

    case_id: str
    script_draft_id: str | None = None
    script_version_id: str | None = None
    rubric_version: int = 1
    composite: float = Field(0.0, ge=0, le=10)
    band: ScoreBand = "ok"
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    reason: str = ""
    locked_at: datetime = Field(default_factory=utcnow)
    settled_reward: float | None = None
    settled_at: datetime | None = None


RewardSourceKind = Literal[
    "draft_adopted",
    "draft_pick",
    "video_produced",
    "published",
    "performance_scored",
    "video_discarded",
    "stale_unpublished",
]

DiscardReason = Literal["script", "visual", "topic", "no_time"]


class RewardSignal(EntityMeta):
    """人类选择 / 阶段进展产生的分级奖励信号（§5），评分卡学习的训练标签。"""

    case_id: str
    script_version_id: str | None = None
    script_draft_id: str | None = None
    source_kind: RewardSourceKind
    value: float = 0.0
    confidence: float = Field(0.5, ge=0, le=1)
    evidence_ref: str | None = None
    reason: DiscardReason | None = None
    occurred_at: datetime = Field(default_factory=utcnow)


class RubricBumpProposal(EntityMeta):
    """评分卡升版提议（§6.4）：新卡须在校准池上重排更准才生成，人工一次确认。"""

    case_id: str
    status: Literal["proposed", "accepted", "rejected"] = "proposed"
    from_version: int = 1
    candidate: CaseRubric
    old_consistency: float = 0.0
    new_consistency: float = 0.0
    sample_size: int = 0
    rationale: str = ""


class CalibrationReport(ContractModel):
    """复盘只读报告（§6.3）：校准池规模、排序一致性、连续误判、待复盘数。"""

    case_id: str
    rubric_version: int = 1
    sample_size: int = 0
    consistency: float | None = None
    miss_streak: int = 0
    pending_retro_count: int = 0
    bump_recommended: bool = False


class MetricsBackfillRequest(ContractModel):
    """单条人工回填（§5.3）：从具体成片/发布进来，无需匹配键，填后台原始计数。

    后端把原始 count 折算成 canonical rate（``counts_to_canonical``）再走与批量
    导入同款的 observation 构建 + ``compute_performance_score``。"""

    window: MetricWindow = "7d"
    platform: str | None = None
    account_id: str | None = None
    views: int | None = None
    impressions: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    follows: int | None = None
    conversions: int | None = None
    avg_watch_sec: float | None = None


class PendingRetroItem(EntityMeta):
    """一条已发布、回填窗口到期但尚未回填指标的成片（"待复盘"）。"""

    case_id: str
    finished_video_id: str
    publish_record_id: str
    video_version_id: str | None = None
    title: str = ""
    platform: str | None = None
    published_at: datetime | None = None
    days_since_publish: int = 0


class PendingRetroResponse(ContractModel):
    case_id: str
    items: list[PendingRetroItem] = Field(default_factory=list)


class RejectBumpRequest(ContractModel):
    reason: str | None = None
