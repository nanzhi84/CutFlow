"""Publishing domain: finished videos, publish packages, editor/Jianying handoff, and publish batches."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import Field, JsonValue

from .base import ArtifactRef, BaseListQuery, ContractModel, EntityMeta, NodeError
from .cases import PublishRecord, VideoVersion


class FinishedVideo(EntityMeta):
    case_id: str
    run_id: str | None = None
    title: str
    video_artifact: ArtifactRef
    cover_artifact: ArtifactRef | None = None
    subtitle_artifact: ArtifactRef | None = None
    duration_sec: float = 0
    qc_status: Literal["pending", "passed", "failed", "warning"] = "pending"
    # LipSync provider attribution (§ HeyGem-primary → VideoReTalk-fallback). Resolved
    # from the run's LipSyncReportArtifact at export time. ``lipsync_provider_id`` is the
    # ProviderProfile.provider_id of the provider that actually produced the lipsynced
    # video (e.g. ``runninghub.heygem`` / ``dashscope.videoretalk``); ``None`` when lipsync
    # was disabled / skipped / sandbox pass-through / report absent. ``lipsync_fallback_used``
    # is True only when the primary provider failed and the fallback produced the video.
    lipsync_provider_id: str | None = None
    lipsync_fallback_used: bool = False
    lipsync_fallback_reason: str | None = None


class FinishedVideoQuery(BaseListQuery):
    case_id: str | None = None
    qc_status: str | None = None


class FinishedVideoDetail(ContractModel):
    finished_video: FinishedVideo
    video_version: VideoVersion | None = None
    publish_records: list[PublishRecord] = Field(default_factory=list)


class PublishDefaults(ContractModel):
    title: str
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    # §23.7 PublishDefaults parity: per-batch publish payload knobs the platform
    # adapter (M6c) consumes. ``tags`` supersedes the legacy ``hashtags`` vestige
    # (kept for backwards-compatibility of stored rows). ``account_group`` drives
    # multi-account routing (which 小V猫 account publishes for this Case).
    mode: Literal["immediate", "scheduled"] = "immediate"
    scheduled_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    location: str | None = None
    account_group: str | None = None


class PublishPackage(EntityMeta):
    case_id: str | None = None
    source_finished_video_id: str | None = None
    upload_artifact_id: str | None = None
    video_artifact: ArtifactRef
    cover_artifact: ArtifactRef | None = None
    platform_defaults: PublishDefaults


class CreateEditorHandoffRequest(ContractModel):
    format: Literal["zip", "folder_manifest"] = "zip"


class EditorHandoffPackageArtifact(ContractModel):
    package_artifact: ArtifactRef
    manifest: dict[str, JsonValue]


class CreateJianyingDraftRequest(ContractModel):
    template_id: str | None = None


class JianyingDraftPackageArtifact(ContractModel):
    package_artifact: ArtifactRef
    draft_manifest: dict[str, JsonValue]


class PublishPackageQuery(BaseListQuery):
    case_id: str | None = None
    source_type: str | None = None


class CreatePublishPackageRequest(ContractModel):
    source_finished_video_id: str | None = None
    upload_artifact_id: str | None = None
    title: str
    description: str = ""


class PatchPublishPackageRequest(ContractModel):
    title: str | None = None
    description: str | None = None
    cover_artifact_id: str | None = None


class DeletePublishResourceRequest(ContractModel):
    reason: str | None = None


class PublishBatchStatus(str, Enum):
    draft = "draft"
    processing = "processing"
    review_ready = "review_ready"
    publishing = "publishing"
    completed = "completed"
    partial_failed = "partial_failed"


class PublishItemStatus(str, Enum):
    uploaded = "uploaded"
    normalizing = "normalizing"
    asr_running = "asr_running"
    copy_running = "copy_running"
    cover_running = "cover_running"
    review_ready = "review_ready"
    manual_review_ready = "manual_review_ready"
    publishing = "publishing"
    published = "published"
    generation_failed = "generation_failed"
    publish_failed = "publish_failed"
    excluded = "excluded"


class PublishAttemptStatus(str, Enum):
    created = "created"
    manual_review_ready = "manual_review_ready"
    scheduled = "scheduled"
    published = "published"
    failed = "failed"


class PublishBatchItemVm(EntityMeta):
    publish_package_id: str
    platform: str
    title: str
    description: str = ""
    selected: bool = True
    status: PublishItemStatus = PublishItemStatus.uploaded
    # §28.1 PublishBatchItem parity: copy + cover + platform-payload fields the
    # copy/cover nodes and the publish adapter populate.
    publish_content: str = ""
    cover_title: str = ""
    cover_subtitle: str = ""
    cover_artifact_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    location: str | None = None
    account_group: str | None = None
    scheduled_at: datetime | None = None


class PublishBatchVm(EntityMeta):
    status: PublishBatchStatus = PublishBatchStatus.draft
    items: list[PublishBatchItemVm] = Field(default_factory=list)


class PublishBatchQuery(BaseListQuery):
    status: str | None = None


class CreatePublishBatchRequest(ContractModel):
    publish_package_ids: list[str]
    platform_targets: list[str]


class SubmitPublishBatchRequest(ContractModel):
    dry_run: bool = False
    simulate_publish_failure: bool = False
    # §23.7 publish mode + Asia/Shanghai scheduling. ``scheduled`` requires a
    # future ``scheduled_at`` (validated tz-aware in Asia/Shanghai); the publish
    # adapter applies it on the platform. ``adapter_id`` lets an operator/feature
    # flag override the resolved publish adapter for this submit.
    mode: Literal["immediate", "scheduled"] = "immediate"
    scheduled_at: datetime | None = None
    adapter_id: str | None = None


class PatchPublishItemRequest(ContractModel):
    title: str | None = None
    description: str | None = None
    selected: bool | None = None
    publish_content: str | None = None
    cover_title: str | None = None
    cover_subtitle: str | None = None
    cover_artifact_id: str | None = None
    tags: list[str] | None = None
    location: str | None = None
    account_group: str | None = None
    scheduled_at: datetime | None = None


class GeneratePublishCopyRequest(ContractModel):
    """Drive the Publishing Copy Node for one item (§2.1 / §28.3 generate-copy)."""

    overwrite: bool = True
    title_limit: int | None = None


class PublishCopyResult(ContractModel):
    title: str
    publish_content: str
    cover_title: str
    cover_subtitle: str
    source: Literal["llm", "deterministic"]
    prompt_invocation_id: str | None = None


class GeneratePublishCoverRequest(ContractModel):
    """Drive the publishing Cover Node for one item (§2.1 / §28.3 generate-cover).

    ``mode`` selects AI cover vs frame cover. ``frame_time_sec`` is the source
    frame used when AI is unavailable / for ``mode='frame'``."""

    mode: Literal["ai", "frame"] = "ai"
    frame_time_sec: float = 0.0


class PublishCoverResult(ContractModel):
    cover_artifact: ArtifactRef
    source: Literal["ai", "frame"]
    # §2.2: surfaced when an AI cover was requested but fell back to a frame cover.
    frame_fallback: bool = False
    degraded_reason: str | None = None


class PreviewCoverFrameRequest(ContractModel):
    """Operator preview of a source frame at a chosen time (§28.3 preview-cover-frame)."""

    frame_time_sec: float = 0.0


class PreviewCoverFrameResult(ContractModel):
    frame_artifact: ArtifactRef
    frame_time_sec: float


class PlatformAccount(ContractModel):
    """A publish account discoverable through the platform adapter (§28.3
    platform-accounts). UNVERIFIED against the live 小V猫 app; the sandbox adapter
    returns a deterministic stub set."""

    uid: str
    platform: str
    nickname: str = ""
    remark: str = ""
    sub_name: str = ""
    account_group: str | None = None
    is_login: bool = False


class PlatformAccountList(ContractModel):
    adapter_id: str
    accounts: list[PlatformAccount] = Field(default_factory=list)
    available: bool = True
    unavailable_reason: str | None = None


class PublishAttempt(EntityMeta):
    batch_id: str
    item_id: str
    platforms: list[str]
    manual_review: bool = False
    status: PublishAttemptStatus = PublishAttemptStatus.created
    adapter_id: str
    external_task_id: str | None = None
    results: list[dict[str, JsonValue]] = Field(default_factory=list)
    error: NodeError | None = None
    finished_at: datetime | None = None


class PublishAttemptDetail(ContractModel):
    attempt: PublishAttempt
    record: PublishRecord | None = None
