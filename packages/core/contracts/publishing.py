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


class PatchPublishItemRequest(ContractModel):
    title: str | None = None
    description: str | None = None
    selected: bool | None = None


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
