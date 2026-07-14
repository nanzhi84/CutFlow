"""Foundational primitives: base model, shared enums, money, envelopes, and the artifact/error/event core."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ErrorCode(str, Enum):
    validation_missing_case = "validation.missing_case"
    validation_missing_voice = "validation.missing_voice"
    validation_missing_script = "validation.missing_script"
    validation_invalid_options = "validation.invalid_options"
    validation_conflict = "validation.conflict"
    auth_unauthorized = "auth.unauthorized"
    auth_forbidden = "auth.forbidden"
    auth_invalid_credentials = "auth.invalid_credentials"
    auth_registration_closed = "auth.registration_closed"
    auth_user_disabled = "auth.user_disabled"
    upload_invalid_state = "upload.invalid_state"
    upload_size_mismatch = "upload.size_mismatch"
    upload_sha256_mismatch = "upload.sha256_mismatch"
    upload_unsupported_type = "upload.unsupported_type"
    upload_too_large = "upload.too_large"
    upload_normalization_failed = "upload.normalization_failed"
    material_insufficient_portrait = "material.insufficient.portrait"
    material_insufficient_broll = "material.insufficient.broll"
    material_annotation_failed = "material.annotation_failed"
    prompt_render_error = "prompt.render_error"
    prompt_output_invalid = "prompt.output_invalid"
    prompt_version_not_published = "prompt.version_not_published"
    provider_unsupported_option = "provider.unsupported_option"
    provider_quota_exceeded = "provider.quota_exceeded"
    provider_timeout = "provider.timeout"
    provider_remote_failed = "provider.remote_failed"
    provider_circuit_open = "provider.circuit_open"
    provider_auth_failed = "provider.auth_failed"
    provider_cost_unpriced = "provider.cost_unpriced"
    provider_submit_outcome_unknown = "provider.submit_outcome_unknown"
    artifact_missing = "artifact.missing"
    artifact_schema_mismatch = "artifact.schema_mismatch"
    workflow_invalid_transition = "workflow.invalid_transition"
    workflow_worker_lost = "workflow.worker_lost"
    render_invalid_timeline = "render.invalid_timeline"
    render_failed = "render.failed"
    render_subtitle_failed = "render.subtitle_failed"
    publish_failed = "publish.failed"
    import_failed = "import.failed"
    reference_unreachable = "reference.unreachable"
    reference_unsupported_platform = "reference.unsupported_platform"
    reference_asr_failed = "reference.asr_failed"
    reference_cookie_invalid = "reference.cookie_invalid"
    idempotency_conflict = "idempotency.conflict"


class WarningCode(str, Enum):
    broll_skipped_no_material = "broll.skipped_no_material"
    bgm_skipped_library_unannotated = "bgm.skipped_library_unannotated"
    font_default_used = "font.default_used"
    cover_frame_fallback = "cover.frame_fallback"
    timestamp_estimated = "timestamp.estimated"
    cost_unpriced = "cost.unpriced"
    budget_exceeded = "budget.exceeded"
    lipsync_fallback_used = "lipsync.fallback_used"
    bgm_loudness_probe_failed = "bgm.loudness_probe_failed"
    font_resolution_failed = "font.resolution_failed"
    subtitle_burn_skipped = "subtitle.burn_skipped"
    editing_agent_deterministic_fallback = "editing_agent.deterministic_fallback"
    editing_agent_llm_repair = "editing_agent.llm_repair"
    editing_agent_local_constraint_repair = "editing_agent.local_constraint_repair"
    media_selection_agent_deterministic_fallback = "media_selection_agent.deterministic_fallback"
    media_selection_agent_llm_repair = "media_selection_agent.llm_repair"
    media_selection_agent_local_constraint_repair = "media_selection_agent.local_constraint_repair"
    broll_insertions_dropped_geometry = "broll.insertions_dropped_geometry"
    window_query_template_fallback = "window_query.template_fallback"
    huazi_animation_fallback = "huazi.animation_fallback"
    huazi_planning_failed = "huazi.planning_failed"
    font_metrics_fallback = "font.metrics_fallback"
    caption_visual_analysis_failed = "caption.visual_analysis_failed"
    caption_emphasis_relaxed_safety = "caption.emphasis_relaxed_safety"
    caption_emphasis_below_floor = "caption.emphasis_below_floor"
    tts_timing_unavailable = "tts.timing_unavailable"
    postprocess_planning_failed = "postprocess.planning_failed"
    sfx_asset_missing = "sfx.asset_missing"
    sfx_mix_failed = "sfx.mix_failed"


class DegradationCode(str, Enum):
    broll_skipped_no_material = "broll.skipped_no_material"
    bgm_skipped_library_unannotated = "bgm.skipped_library_unannotated"
    font_default_used = "font.default_used"
    cover_frame_fallback = "cover.frame_fallback"
    lipsync_fallback_used = "lipsync.fallback_used"
    bgm_loudness_probe_failed = "bgm.loudness_probe_failed"
    font_resolution_failed = "font.resolution_failed"
    subtitle_burn_skipped = "subtitle.burn_skipped"
    broll_insertions_dropped_geometry = "broll.insertions_dropped_geometry"
    editing_agent_deterministic_fallback = "editing_agent.deterministic_fallback"
    media_selection_agent_deterministic_fallback = "media_selection_agent.deterministic_fallback"
    window_query_template_fallback = "window_query.template_fallback"
    timestamp_estimated = "timestamp.estimated"
    huazi_animation_fallback = "huazi.animation_fallback"
    huazi_planning_failed = "huazi.planning_failed"
    font_metrics_fallback = "font.metrics_fallback"
    caption_visual_analysis_failed = "caption.visual_analysis_failed"
    caption_emphasis_relaxed_safety = "caption.emphasis_relaxed_safety"
    caption_emphasis_below_floor = "caption.emphasis_below_floor"
    tts_timing_unavailable = "tts.timing_unavailable"
    postprocess_planning_failed = "postprocess.planning_failed"
    sfx_asset_missing = "sfx.asset_missing"
    sfx_mix_failed = "sfx.mix_failed"


class JobStatus(str, Enum):
    draft = "draft"
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    archived = "archived"


class RunStatus(str, Enum):
    created = "created"
    admitted = "admitted"
    running = "running"
    cancelling = "cancelling"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class NodeStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    degraded = "degraded"
    cancelled = "cancelled"


class ProviderStatus(str, Enum):
    prepared = "prepared"
    submitted = "submitted"
    polling = "polling"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"


class JobType(str, Enum):
    digital_human_video = "digital_human_video"
    publish_batch = "publish_batch"
    annotation_batch = "annotation_batch"


class ArtifactKind(str, Enum):
    uploaded_file = "uploaded.file"
    validated_production_spec = "spec.validated_production"
    case_context = "case.context"
    creative_intent = "creative.intent"
    audio_tts = "audio.tts"
    audio_alignment_raw = "audio.alignment.raw"
    audio_alignment = "audio.alignment"
    narration_units = "narration.units"
    plan_material_pack = "plan.material_pack"
    plan_narration_boundary = "plan.narration_boundary"
    plan_timeline_windows = "plan.timeline_windows"
    plan_window_queries = "plan.window_queries"
    plan_window_material_retrieval = "plan.window_material_retrieval"
    plan_media_assignment = "plan.media_assignment"
    plan_portrait = "plan.portrait"
    plan_broll = "plan.broll"
    plan_style = "plan.style"
    plan_timeline = "plan.timeline"
    plan_render = "plan.render"
    plan_editing_diagnostics = "plan.editing_diagnostics"
    plan_media_selection_diagnostics = "plan.media_selection_diagnostics"
    plan_caption_windows = "plan.caption_windows"
    plan_postprocess_diagnostics = "plan.postprocess_diagnostics"
    plan_caption_display = "plan.caption_display"
    video_portrait_track = "video.portrait_track"
    video_lipsync = "video.lipsync"
    lipsync_report = "lipsync.report"
    video_rendered = "video.rendered"
    video_final = "video.final"
    video_finished = "video.finished"
    subtitle_ass = "subtitle.ass"
    cover_image = "cover.image"
    # Small WebP derivative of cover_image, for list/grid cards. The full-size
    # cover stays the publishing asset; this is what the browser downloads.
    cover_thumbnail = "cover.thumbnail"
    publish_package = "publish.package"
    run_report_public = "run.report.public"
    run_report_debug = "run.report.debug"
    editor_handoff = "editor.handoff_package"
    jianying_draft = "editor.jianying_draft_package"
    provider_raw_request = "provider.raw_request"
    provider_raw_response = "provider.raw_response"
    material_annotation = "material.annotation"


class Money(ContractModel):
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    amount_micro: int | None = None

    @model_validator(mode="after")
    def _derive_amount_micro(self) -> "Money":
        # Spec 23.1: persisted money must always carry amount_micro to avoid float drift.
        if self.amount_micro is None:
            object.__setattr__(self, "amount_micro", int(self.amount * 1_000_000))
        return self


def zero_money(currency: str = "CNY") -> Money:
    return Money(amount=Decimal("0"), currency=currency, amount_micro=0)


class EntityMeta(ContractModel):
    id: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None
    version: int = 1
    schema_version: str = "v1"


class OkResponse(ContractModel):
    ok: bool = True
    request_id: str


class PageResponse(ContractModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    total_hint: int | None = None
    request_id: str


class SignedUrlResponse(ContractModel):
    url: str
    expires_at: datetime
    request_id: str
    content_type: str | None = None
    playable: bool = False


class EventStreamTokenResponse(ContractModel):
    stream_url: str
    token: str
    expires_at: datetime
    request_id: str


class RunEvent(ContractModel):
    event_id: str
    run_id: str
    job_id: str
    event_type: Literal["run_update", "node_update", "artifact_created", "warning", "error"]
    node_id: str | None = None
    status: str | None = None
    progress: float | None = None
    message: str
    created_at: datetime


class NodeError(ContractModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    severity: Literal["info", "warning", "error", "fatal"] = "error"
    details: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    node_run_id: str | None = None


class DegradationNotice(ContractModel):
    code: WarningCode
    message: str
    node_id: str | None = None
    policy_id: str | None = None
    affects_true_yield: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ErrorEnvelope(ContractModel):
    error: NodeError


class ArtifactRef(ContractModel):
    artifact_id: str
    kind: ArtifactKind
    uri: str
    schema_version: str = "v1"
    sha256: str | None = None


class MediaInfo(ContractModel):
    media_type: Literal["video", "audio", "image", "subtitle", "json"]
    codec: str
    format: str
    mime_type: str | None = None
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    # Color metadata for video streams. ``is_hdr`` is True when the source uses a
    # BT.2020 primaries or a PQ/HLG transfer curve and therefore needs a
    # tonemap to BT.709 before it can be rendered/thumbnailed without color
    # degradation. ``None`` means "not a video / not probed".
    color_transfer: str | None = None
    color_primaries: str | None = None
    is_hdr: bool | None = None


class Artifact(EntityMeta):
    case_id: str | None = None
    run_id: str | None = None
    node_run_id: str | None = None
    kind: ArtifactKind
    uri: str | None = None
    local_path: str | None = None
    oss_uri: str | None = None
    size_bytes: int | None = None
    immutable: bool = True
    retention_policy: str = "default"
    sha256: str | None = None
    media_info: MediaInfo | None = None
    payload_schema: str
    payload: JsonValue | None = None
    created_by_node_run_id: str | None = None


class RetryPolicy(ContractModel):
    max_attempts: int = Field(1, ge=1, le=10)
    backoff_seconds: float = Field(0, ge=0)
    backoff_multiplier: float = Field(2.0, ge=1.0)
    retryable_error_codes: list[ErrorCode] = Field(default_factory=list)
