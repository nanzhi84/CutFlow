"""Media domain: uploads, media assets, selection ledger, annotation editing, and voices."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Literal
from uuid import uuid4
from pydantic import Field, JsonValue

from .base import ArtifactRef, BaseListQuery, ContractModel, EntityMeta, ErrorCode, utcnow
from .publishing import PublishPackage


class UploadSessionStatus(str, Enum):
    prepared = "prepared"
    uploading = "uploading"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    expired = "expired"


UploadStatus = UploadSessionStatus


class UploadKind(str, Enum):
    portrait = "portrait"
    broll = "broll"
    voice_reference = "voice_reference"
    bgm = "bgm"
    font = "font"
    cover_template = "cover_template"
    publish_video = "publish_video"


class PrepareUploadRequest(ContractModel):
    kind: UploadKind
    case_id: str | None = None
    filename: str
    content_type: str
    size_bytes: int = Field(gt=0)
    sha256: str | None = None
    multipart: bool = False
    stabilize: bool = False


class UploadSession(EntityMeta):
    kind: UploadKind
    case_id: str | None = None
    filename: str
    content_type: str
    size_bytes: int
    sha256: str | None = None
    status: UploadStatus = UploadStatus.prepared
    upload_url: str | None = None
    local_temp_path: str | None = None
    object_uri: str | None = None
    stabilize: bool = False
    stabilized: bool = False
    expires_at: datetime = Field(default_factory=lambda: utcnow() + timedelta(hours=1))


class CompleteUploadRequest(ContractModel):
    upload_session_id: str
    size_bytes: int | None = None
    sha256: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class CompleteUploadResponse(ContractModel):
    upload_session: UploadSession
    artifact: ArtifactRef
    media_asset: MediaAssetRecord | None = None
    publish_package: PublishPackage | None = None
    request_id: str


class MediaAssetRecord(EntityMeta):
    case_id: str | None = None
    title: str
    kind: Literal[
        "portrait",
        "broll",
        "bgm",
        "font",
        "cover_template",
        "voice_reference",
        "voice",
        "video",
        "image",
        "other",
    ]
    source_artifact_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    annotation_status: Literal["pending", "annotated", "annotation_failed"] = "pending"
    usable: bool = True


SelectionMedium = Literal["portrait", "broll", "bgm", "font"]


class SelectionLedgerEntry(ContractModel):
    id: str = Field(default_factory=lambda: f"sel_{uuid4().hex[:12]}")
    case_id: str
    run_id: str
    medium: SelectionMedium
    asset_id: str
    slot_phase: str
    diversity_key: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class MaterialUsageRankingItem(ContractModel):
    asset_id: str
    medium: SelectionMedium
    asset: MediaAssetRecord | None = None
    task_use_count: int = 0
    segment_use_count: int = 0
    last_used_at: datetime | None = None
    recent_score: float = 0


class MaterialUsageRankingReport(ContractModel):
    kind: SelectionMedium
    case_id: str | None = None
    top_n: int = Field(20, ge=1, le=100)
    items: list[MaterialUsageRankingItem] = Field(default_factory=list)
    request_id: str = "req_local"


class MediaAssetQuery(BaseListQuery):
    case_id: str | None = None
    kind: str | None = None
    annotation_status: str | None = None


class CreateMediaAssetFromUploadRequest(ContractModel):
    upload_session_id: str
    case_id: str | None = None
    title: str
    tags: list[str] = Field(default_factory=list)
    kind: Literal[
        "portrait",
        "broll",
        "voice_reference",
        "bgm",
        "font",
        "cover_template",
        "video",
        "image",
        "other",
    ] = "other"


class MediaAssetCard(ContractModel):
    asset: MediaAssetRecord
    preview_url: str | None = None
    latest_annotation_id: str | None = None
    badges: list[str] = Field(default_factory=list)


class MediaAssetDetail(ContractModel):
    asset: MediaAssetRecord
    preview_url: str | None = None
    latest_annotation_id: str | None = None


class BatchStabilizeMediaAssetsRequest(ContractModel):
    asset_ids: list[str] = Field(min_length=1, max_length=50)


class MediaAssetProcessingResult(ContractModel):
    asset_id: str
    status: Literal["completed", "failed"]
    artifact_id: str | None = None
    error_code: ErrorCode | None = None
    message: str | None = None


class BatchMediaProcessResponse(ContractModel):
    results: list[MediaAssetProcessingResult]
    request_id: str


class TimelineSegment(ContractModel):
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)


class TrimAnnotationRequest(ContractModel):
    valid_segments: list[TimelineSegment] | None = None


class TrimAnnotationResponse(ContractModel):
    asset_id: str
    artifact: ArtifactRef
    valid_duration_sec: float
    request_id: str


class MediaAssetReplaceSourceRequest(ContractModel):
    upload_session_id: str


class MediaAssetReplaceResponse(ContractModel):
    asset: MediaAssetRecord
    artifact: ArtifactRef
    preserved_annotation: bool
    request_id: str


class AutoMatchReplaceRequest(ContractModel):
    upload_session_ids: list[str] = Field(min_length=1, max_length=100)
    case_id: str | None = None
    kind: str = "broll"


class AutoMatchReplaceResult(ContractModel):
    upload_session_id: str
    filename: str
    status: Literal["matched", "unmatched", "ambiguous", "failed"]
    asset_id: str | None = None
    artifact_id: str | None = None
    message: str | None = None


class AutoMatchReplaceResponse(ContractModel):
    results: list[AutoMatchReplaceResult]
    request_id: str


class AnnotationPatch(ContractModel):
    operations: list[dict[str, JsonValue]] = Field(default_factory=list)


class PatchAnnotationRequest(ContractModel):
    etag: str
    patch: AnnotationPatch


class RerunAnnotationRequest(ContractModel):
    provider_profile_id: str | None = None
    force: bool = False


class AnnotationRunResponse(ContractModel):
    asset_id: str
    run_id: str | None
    status: Literal["queued", "running", "completed", "failed"]


class AnnotationEditorVm(ContractModel):
    asset: MediaAssetRecord
    etag: str
    canonical: dict[str, JsonValue]
    projection: dict[str, JsonValue]
    editable_paths: list[str] = Field(default_factory=list)


class VoiceProfile(EntityMeta):
    display_name: str
    source: Literal["builtin", "cloned", "designed"]
    provider_profile_id: str | None = None
    preview_artifact_id: str | None = None
    enabled: bool = True


class VoiceQuery(BaseListQuery):
    source: str | None = None
    enabled: bool | None = None


class CloneVoiceRequest(ContractModel):
    display_name: str
    reference_upload_session_id: str
    provider_profile_id: str | None = None


class DesignVoiceRequest(ContractModel):
    display_name: str
    prompt: str
    provider_profile_id: str | None = None


class VoicePreviewRequest(ContractModel):
    text: str
    provider_profile_id: str | None = None


class VoicePreviewResponse(ContractModel):
    voice_id: str
    audio_artifact: ArtifactRef
    duration_sec: float


class PatchVoiceRequest(ContractModel):
    display_name: str | None = None
    enabled: bool | None = None
