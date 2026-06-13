"""Media domain: uploads, media assets, selection ledger, annotation editing, and voices."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal
from uuid import uuid4
from pydantic import Field, JsonValue, model_validator

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


# ===========================================================================
# Annotation V4 contracts (seven-layer unified annotation) + sensor artifacts.
#
# These are the artifact shapes the pure CV/VAD/scene-detection sensor suite
# feeds, and which downstream b-roll planning consumes. Portrait and b-roll
# share one schema; semantic fields are a unified superset, each optional and
# filled per material_type. Strict validators are the quality-gate safety net:
# illegal time ranges raise rather than silently coerce. The VLM-driven
# annotation pipeline (a later step) populates the semantic layers; the sensors
# ported here populate shot cuts, speech islands, quality events, windows, and
# the deterministic quality report.
# ===========================================================================


class AnnotationVersion(str, Enum):
    """Structured annotation protocol version. V4 is the only protocol in use."""

    v4 = "annotation_v4"


class UsageRole(str, Enum):
    """Clip role (single choice).

    hook   = opening hook; main = main talking-head body; backup = spare;
    avoid  = do not use; cover = b-roll used to cover voiceover.
    """

    hook = "hook"
    main = "main"
    backup = "backup"
    avoid = "avoid"
    cover = "cover"


class QualityEventType(str, Enum):
    """Explicit quality-event types.

    The first eight are detected by sensors/VLM; ``manual_note`` is a free-form
    annotation added in the editor and never participates in automatic scoring.
    Deterministic sensors here emit ``occlusion`` (black/freeze), ``blur``,
    ``shake``, and ``camera_drop``.
    """

    blooper_laugh = "blooper_laugh"
    camera_drop = "camera_drop"
    shake = "shake"
    blur = "blur"
    look_off_camera = "look_off_camera"
    exit_frame = "exit_frame"
    retake_pause = "retake_pause"
    occlusion = "occlusion"
    manual_note = "manual_note"


class AnnotationStatus(str, Enum):
    """Annotation lifecycle. V4 terminal states are only completed / failed."""

    pending = "pending"
    analyzing = "analyzing"
    completed = "completed"
    failed = "failed"


class ClipSemanticsV4(ContractModel):
    """Clip semantics (unified superset).

    Portrait and b-roll semantic fields coexist and are each optional; fill the
    side matching material_type and leave the other at its default so downstream
    faces a single schema without branching.
    """

    # --- shared ---
    subject_type: str = ""
    scene_type: str = ""

    # --- portrait (talking-head) ---
    gaze_to_camera: bool | None = None
    mouth_visible: bool | None = None
    mouth_moving: bool | None = None
    gesture_type: str = ""
    body_orientation: str = ""
    emotion_state: str = ""
    speaker_intent: str = ""
    speech_action_alignment: str = ""
    retake_cue: str = ""

    # --- b-roll (scenery / product) ---
    action: str = ""
    narrative_role: str = ""
    contains_face: bool | None = None
    face_count_max: int | None = Field(
        None,
        description="Max faces in a single frame (incl. mirror/reflection/screen/background); >1 means not lip-sync usable",
    )
    process_stage: str = ""


class ClipVisualV4(ContractModel):
    """Clip visual layer. Shot scale is a single field."""

    shot_scale: str = ""
    camera_motion: str = ""
    composition: str = ""


class ClipUsageV4(ContractModel):
    """Clip usability + role."""

    recommended_for_lip_sync: bool = False
    recommended_for_voiceover: bool = False
    voiceover_only: bool = False
    role: UsageRole


class ClipRetrievalV4(ContractModel):
    """Clip retrieval view (the single canonical summary)."""

    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    retrieval_sentence: str = ""


class ClipV4(ContractModel):
    """V4 editable clip. Time-consistent plus semantic/visual/usage/retrieval sub-layers."""

    segment_id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    duration: float = Field(ge=0)
    semantics: ClipSemanticsV4 = Field(default_factory=ClipSemanticsV4)
    visual: ClipVisualV4 = Field(default_factory=ClipVisualV4)
    usage: ClipUsageV4
    retrieval: ClipRetrievalV4 = Field(default_factory=ClipRetrievalV4)
    confidence: float = Field(0.8, ge=0, le=1)

    @model_validator(mode="after")
    def _validate_time(self) -> "ClipV4":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be greater than start ({self.start})")
        derived = round(self.end - self.start, 3)
        if abs(derived - self.duration) > 0.12:
            self.duration = derived
        return self


class QualityEventV4(ContractModel):
    """V4 quality event (the single authoritative risk source).

    ``source`` distinguishes 'sensor' (black/freeze/blur/shake/camera_drop) from
    'vlm' (blooper/look-off). Deterministic sensors here set source='sensor' or
    'motion_guard'.
    """

    event_id: str
    event_type: QualityEventType
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    description: str = ""
    risk_tier: str = "hard"
    confidence: float = Field(0.0, ge=0, le=1)
    severity: float = Field(0.0, ge=0, le=1)
    source: str | None = None
    segment_id: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> "QualityEventV4":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be greater than start ({self.start})")
        tier = str(self.risk_tier or "").strip().lower()
        if tier not in {"soft", "hard"}:
            raise ValueError(f"risk_tier must be soft or hard, got: {self.risk_tier!r}")
        self.risk_tier = tier
        return self


class UsageWindowV4(ContractModel):
    """V4 recommended clip window."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    role: UsageRole
    reason: str = ""
    confidence: float = Field(0.0, ge=0, le=1)

    @model_validator(mode="after")
    def _validate_time(self) -> "UsageWindowV4":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be greater than start ({self.start})")
        return self


class AnnotationMetaV4(ContractModel):
    """V4 meta layer."""

    annotation_version: AnnotationVersion = AnnotationVersion.v4
    asset_id: str
    case_id: str
    material_type: str
    duration: float = Field(0.0, ge=0)
    generated_at: str | None = None
    annotation_status: AnnotationStatus = AnnotationStatus.completed


class AnnotationV4(ContractModel):
    """V4 unified annotation (seven-layer clean view).

    The editing agent consumes only this interface; portrait and b-roll share
    one structure. All time-bearing layers must fall inside [0, duration] (out
    of bounds raises, the quality-gate safety net). duration<=0 skips the upper
    bound check (unknown duration / empty annotation is legal).
    """

    meta: AnnotationMetaV4
    clips: list[ClipV4] = Field(default_factory=list)
    quality_events: list[QualityEventV4] = Field(default_factory=list)
    quality_report: dict[str, Any] = Field(default_factory=dict)
    usage_windows: list[UsageWindowV4] = Field(default_factory=list)
    evidence_frames: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_time_bounds(self) -> "AnnotationV4":
        duration = self.meta.duration
        if duration and duration > 0:
            upper = duration + 1e-6
            for clip in self.clips:
                if clip.start < 0 or clip.end > upper:
                    raise ValueError(
                        f"clip {clip.segment_id} time [{clip.start}, {clip.end}] "
                        f"out of bounds [0, {duration}]"
                    )
            for ev in self.quality_events:
                if ev.start < 0 or ev.end > upper:
                    raise ValueError(
                        f"quality_event {ev.event_id} time [{ev.start}, {ev.end}] "
                        f"out of bounds [0, {duration}]"
                    )
            for win in self.usage_windows:
                if win.start < 0 or win.end > upper:
                    raise ValueError(
                        f"usage_window time [{win.start}, {win.end}] out of bounds [0, {duration}]"
                    )
            for ts in self.evidence_frames:
                if ts < 0 or ts > upper:
                    raise ValueError(f"evidence_frame {ts} out of bounds [0, {duration}]")
        return self


# --- Sensor-layer artifact shapes (deterministic CV/VAD/scene-detection outputs) ---


class WindowReason(str, Enum):
    """Source label for a planned analysis window boundary."""

    scene_boundary = "scene_boundary"
    merged_short = "merged_short"
    long_scene_split = "long_scene_split"
    mechanical = "mechanical"
    vad_snapped = "vad_snapped"


class AnalysisWindow(ContractModel):
    """A bounded analysis window. Times are seconds relative to the asset start."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    reason: WindowReason = WindowReason.scene_boundary

    @model_validator(mode="after")
    def _validate_time(self) -> "AnalysisWindow":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be greater than start ({self.start})")
        return self


class SpeechIslandV4(ContractModel):
    """A contiguous voice-activity span detected by the VAD sensor.

    confidence is the mean speech probability over the span (0..1).
    """

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    confidence: float = Field(0.0, ge=0, le=1)

    @model_validator(mode="after")
    def _validate_time(self) -> "SpeechIslandV4":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be greater than start ({self.start})")
        return self


class PortraitQualityReport(ContractModel):
    """Deterministic whole-clip health report for portrait (talking-head) material."""

    hook_strength: str
    speech_stability: str
    tail_state: str
    lip_sync_suitability_score: int = Field(ge=0, le=100)


class BrollQualityReport(ContractModel):
    """Deterministic whole-clip health report for b-roll (scenery) material."""

    usable_ratio: float = Field(ge=0, le=1)
    stability_score: float = Field(ge=0, le=100)
    hard_quality_count: int = Field(ge=0)
    soft_quality_count: int = Field(ge=0)
    dominant_scene_types: list[str] = Field(default_factory=list)
    dominant_shot_scales: list[str] = Field(default_factory=list)
