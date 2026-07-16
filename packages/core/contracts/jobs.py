"""Jobs & runs domain: workflow templates, job/run requests, run lifecycle, and run reports."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from pydantic import Field, JsonValue, model_validator

from .base import ArtifactKind, ArtifactRef, ContractModel, DegradationCode, DegradationNotice, EntityMeta, JobStatus, JobType, NodeError, NodeStatus, RetryPolicy, RunStatus, WarningCode


class WorkflowEdge(ContractModel):
    from_node_id: str
    to_node_id: str
    condition: str | None = None


class NodeSpec(ContractModel):
    node_id: str
    node_version: str = "v1"
    output_artifact_kinds: list[ArtifactKind]
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    reuse_policy: Literal["strict", "never"] = "strict"
    side_effects: list[
        Literal[
            "provider_call",
            "selection_reservation",
            "domain_write",
            "ledger_commit",
            "external_upload",
            "publish_attempt",
        ]
    ] = Field(default_factory=list)
    idempotency_key: str | None = None


class WorkflowTemplate(ContractModel):
    workflow_template_id: str
    version: str
    nodes: list[NodeSpec]
    edges: list[WorkflowEdge] = Field(default_factory=list)


class VoiceOptions(ContractModel):
    voice_id: str
    provider_profile_id: str | None = None
    speed: float = Field(1.0, ge=0.5, le=2.0)
    emotion: str = "neutral"
    volume: float = Field(1.0, ge=0.0, le=2.0)


class BrollOptions(ContractModel):
    enabled: bool = True
    mode: Literal["insert", "full_coverage"] = "insert"
    case_id: str | None = None
    max_inserts: int = Field(4, ge=0, le=20)
    min_segment_duration: float = Field(3.0, ge=0.5)
    # When true, person-free clean-cover clips that share NO keyword with the
    # narration may still be used as generic b-roll fillers (the keyword
    # relevance floor is bypassed for them — never the person/lip-sync gates).
    # Default-on so the standard digital_human_v2 flow stops soft-degrading to
    # empty b-roll merely because no clip literally matches the script; set
    # false to require keyword relevance. Full-coverage mode still follows this
    # option; it only changes whether every compiled B-roll window is required.
    allow_generic_coverage: bool = True


class LipSyncOptions(ContractModel):
    enabled: bool = True
    provider_profile_id: str = "runninghub.heygem.default"
    timeout_minutes: int = Field(30, ge=5, le=120)


class SubtitleOptions(ContractModel):
    enabled: bool = True
    normal_enabled: bool = True
    emphasis_enabled: bool = True
    style_preset: str = "douyin"
    font_id: str | None = None
    emphasis_font_id: str | None = None
    font_size: int | None = None
    emphasis_font_size: int | None = None
    emphasis_primary_color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    emphasis_style_id: str | None = None
    position: dict[str, float] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_disabled_caption_shorthand(cls, value):
        if (
            isinstance(value, dict)
            and value.get("enabled") is False
            and "emphasis_enabled" not in value
        ):
            return {**value, "normal_enabled": False, "emphasis_enabled": False}
        return value

    @model_validator(mode="after")
    def validate_caption_layers(self) -> "SubtitleOptions":
        if self.emphasis_enabled and (not self.enabled or not self.normal_enabled):
            raise ValueError("emphasis subtitles require normal subtitles")
        return self


class BgmOptions(ContractModel):
    enabled: bool = True
    bgm_id: str | None = None
    volume: float = Field(0.25, ge=0, le=1)
    auto_mix: bool = True


class CoverOptions(ContractModel):
    # AI cover is the default; the frame-extracted cover is the honest fallback when
    # AI is unavailable (no real image.generate profile / secret) or the paid call fails.
    mode: Literal["none", "frame", "ai"] = "ai"
    # Selects the image.generate ProviderProfile for the AI cover (when None,
    # eligible real profiles are tried in failover order). NOT a media asset id.
    template_id: str | None = None
    # Optional uploaded ``cover_template`` MediaAsset whose image conditions the AI
    # cover's style/layout: its bytes are passed to the image-edit reference path so
    # the generated cover follows the uploaded reference, not pure text-to-image.
    reference_asset_id: str | None = None


class OutputOptions(ContractModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30


class StrictnessOptions(ContractModel):
    strict_timestamps: bool = True
    portrait_insufficient_policy: Literal["hard_fail"] = "hard_fail"


class EditPlanningOptions(ContractModel):
    """Per-video extra editing steering for the LLM editing-agent template.

    Consumed by ``digital_human_editing_agent_v2``'s ``MediaSelectionAgentPlanning``
    node; ignored by every other template. ``instruction`` is a free-text hint the
    editing agent honours for this one video (e.g. "尽量用穿搭相近的人像素材，B-roll
    只在讲施工细节时出现")。``max_repair_attempts`` bounds how many times an invalid
    LLM selection is re-prompted before the node fail-fasts (0 = no repair).
    """

    instruction: str = ""
    max_repair_attempts: int = Field(1, ge=0, le=3)


class DigitalHumanVideoRequest(ContractModel):
    schema_version: Literal["digital_human_video_request.v1"] = "digital_human_video_request.v1"
    case_id: str
    script: str
    title: str | None = None
    publish_content: str = ""
    script_version_id: str | None = None
    creative_intent_ref: ArtifactRef | None = None
    workflow_template_id: str = "digital_human_v2"
    # Seedance (video.generate) reference-image asset ids; ignored by other
    # templates. Empty = pure text-to-video. The SeedanceGenerateVideo node
    # resolves each id to its source artifact uri and presigns it for the vendor.
    reference_asset_ids: list[str] = Field(default_factory=list)
    voice: VoiceOptions = Field(default_factory=VoiceOptions)
    broll: BrollOptions = Field(default_factory=BrollOptions)
    lipsync: LipSyncOptions = Field(default_factory=LipSyncOptions)
    subtitle: SubtitleOptions = Field(default_factory=SubtitleOptions)
    bgm: BgmOptions = Field(default_factory=BgmOptions)
    cover: CoverOptions = Field(default_factory=CoverOptions)
    output: OutputOptions = Field(default_factory=OutputOptions)
    strictness: StrictnessOptions = Field(default_factory=StrictnessOptions)
    edit: EditPlanningOptions = Field(default_factory=EditPlanningOptions)


class BatchItemOverrides(ContractModel):
    """Per-item option overrides for batch generation (a subset of
    ``DigitalHumanVideoRequest``'s option blocks). Any block left ``None`` falls
    back to the merge chain (my-defaults -> system default)."""

    voice: VoiceOptions | None = None
    broll: BrollOptions | None = None
    lipsync: LipSyncOptions | None = None
    subtitle: SubtitleOptions | None = None
    bgm: BgmOptions | None = None
    cover: CoverOptions | None = None
    output: OutputOptions | None = None
    strictness: StrictnessOptions | None = None
    workflow_template_id: str | None = None


class BatchItem(ContractModel):
    """One script in a batch request. ``overrides`` win over the user's saved
    defaults, which in turn win over the per-block system defaults."""

    script: str
    title: str | None = None
    publish_content: str | None = None
    script_version_id: str | None = None
    overrides: BatchItemOverrides | None = None


class BatchDigitalHumanVideoRequest(ContractModel):
    schema_version: Literal["batch_digital_human_video_request.v1"] = (
        "batch_digital_human_video_request.v1"
    )
    case_id: str
    items: list[BatchItem] = Field(min_length=1, max_length=50)
    # When True (default), the caller's saved generation defaults are merged
    # underneath each item's overrides; when False, only system defaults apply.
    use_my_defaults: bool = True


class BatchItemResult(ContractModel):
    index: int
    job_id: str | None = None
    run_id: str | None = None
    status: Literal["created", "queued", "failed"]
    error: str | None = None


class BatchGenerationResponse(ContractModel):
    results: list[BatchItemResult]
    request_id: str = "req_local"


class PublishBatchRequest(ContractModel):
    schema_version: Literal["publish_batch_request.v1"] = "publish_batch_request.v1"
    publish_package_ids: list[str]
    platform_targets: list[str]


class AnnotationBatchRequest(ContractModel):
    schema_version: Literal["annotation_batch_request.v1"] = "annotation_batch_request.v1"
    asset_ids: list[str]
    provider_profile_id: str | None = None
    # When False, assets already in annotation_status=annotated are skipped;
    # when True every asset is (re-)annotated.
    force: bool = True
    material_type: str | None = None


JobRequest = Annotated[
    DigitalHumanVideoRequest | PublishBatchRequest | AnnotationBatchRequest,
    Field(discriminator="schema_version"),
]


class Job(EntityMeta):
    type: JobType
    status: JobStatus = JobStatus.draft
    case_id: str | None = None
    created_by: str | None = None
    request_schema: str
    request: JobRequest
    active_run_id: str | None = None
    latest_finished_video_id: str | None = None


class WorkflowRun(EntityMeta):
    job_id: str
    case_id: str | None = None
    workflow_template_id: str
    workflow_version: str
    status: RunStatus = RunStatus.created
    requested_by: str | None = None
    run_attempt: int = 1
    resume_from_run_id: str | None = None
    retry_of_run_id: str | None = None
    experiment_assignment_id: str | None = None
    public_report_artifact_id: str | None = None
    debug_report_artifact_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class NodeRun(EntityMeta):
    run_id: str
    node_id: str
    node_version: str
    status: NodeStatus
    attempt: int = 1
    input_manifest_hash: str
    output_artifact_ids: list[str] = Field(default_factory=list)
    provider_invocation_ids: list[str] = Field(default_factory=list)
    error: NodeError | None = None
    skipped_reason: str | None = None
    degradation_reason: str | None = None
    warnings: list[WarningCode] = Field(default_factory=list)
    degradations: list[DegradationNotice] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ValidatedProductionSpec(ContractModel):
    request: DigitalHumanVideoRequest
    workflow_template_id: str
    workflow_version: str
    compatible: bool = True


CreateDigitalHumanVideoJobRequest = DigitalHumanVideoRequest


class CreateRunRequest(ContractModel):
    mode: Literal["new", "retry", "resume"] = "new"
    reason: str | None = None


class CancelRunRequest(ContractModel):
    reason: str | None = None
    force: bool = False


class RetryRunRequest(ContractModel):
    reason: str | None = None


class ResumeRunRequest(ContractModel):
    reason: str | None = None
    reuse_valid_artifacts: bool = True


class WorkflowRunResponse(ContractModel):
    run: WorkflowRun
    request_id: str


RetryRunResponse = WorkflowRunResponse


ResumeRunResponse = WorkflowRunResponse


class CreateJobResponse(ContractModel):
    job: Job
    initial_run: WorkflowRun | None
    request_id: str


class JobDetailResponse(ContractModel):
    job: Job
    runs: list[WorkflowRun]
    latest_report_artifact_id: str | None = None
    request_id: str = "req_local"


class RunConfigSummary(ContractModel):
    """User-facing snapshot of the inputs a run was launched with.

    Sourced from the originating job's DigitalHumanVideoRequest so the run detail
    view can show the title, chosen voice, script copy, publish copy, and output
    format without the operator hunting through raw node artifacts.
    """

    run_id: str
    job_id: str
    workflow_template_id: str | None = None
    title: str | None = None
    script: str | None = None
    publish_content: str | None = None
    voice_id: str | None = None
    voice_provider_profile_id: str | None = None
    voice_speed: float | None = None
    voice_emotion: str | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    subtitle_enabled: bool | None = None
    broll_enabled: bool | None = None
    lipsync_enabled: bool | None = None


class RunDetailResponse(ContractModel):
    run: WorkflowRun
    node_runs: list[NodeRun]
    artifacts: list[ArtifactRef]
    artifact_payloads: dict[str, JsonValue] = Field(default_factory=dict)
    config: RunConfigSummary | None = None
    request_id: str = "req_local"


class RunCard(ContractModel):
    run_id: str = Field(alias="runId")
    job_id: str = Field(alias="jobId")
    case_id: str = Field(alias="caseId")
    status: RunStatus
    progress: float = Field(ge=0, le=1)
    current_node_label: str | None = Field(default=None, alias="currentNodeLabel")
    title: str
    preview_url: str | None = Field(default=None, alias="previewUrl")
    warnings: list[str] = Field(default_factory=list)
    can_resume: bool = Field(alias="canResume")
    can_retry: bool = Field(alias="canRetry")
    can_publish: bool = Field(alias="canPublish")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class RunOverviewResponse(ContractModel):
    items: list[RunCard]
    next_cursor: str | None = None
    total_hint: int | None = None
    status_counts: dict[str, int] = Field(default_factory=dict, alias="statusCounts")
    failure_code_counts: dict[str, int] = Field(default_factory=dict, alias="failureCodeCounts")
    degradation_code_counts: dict[str, int] = Field(
        default_factory=dict, alias="degradationCodeCounts"
    )
    request_id: str = "req_local"


class BatchFeasibilityResponse(ContractModel):
    case_id: str = Field(alias="caseId")
    estimated_audio_duration_sec: float = Field(alias="estimatedAudioDurationSec")
    portrait_duration_sec: float = Field(alias="portraitDurationSec")
    clean_broll_candidate_count: int = Field(alias="cleanBrollCandidateCount")
    estimated_broll_window_count: int = Field(alias="estimatedBrollWindowCount")
    portrait_ok: bool = Field(alias="portraitOk")
    broll_ok: bool = Field(alias="brollOk")
    notes: list[str] = Field(default_factory=list)
    request_id: str = "req_local"


class FinishedVideoDownloadItem(ContractModel):
    finished_video_id: str = Field(alias="finishedVideoId")
    title: str | None = None
    url: str
    expires_at: datetime = Field(alias="expiresAt")
    content_type: str = Field(default="video/mp4", alias="contentType")
    filename: str


class BatchFinishedVideoDownloadResponse(ContractModel):
    items: list[FinishedVideoDownloadItem]
    request_id: str = "req_local"


class RunActionResponse(ContractModel):
    run: WorkflowRun
    accepted: bool
    request_id: str = "req_local"


class RunPublicReportArtifact(ContractModel):
    run_id: str
    status: RunStatus
    summary: str
    node_statuses: dict[str, NodeStatus]
    warnings: list[WarningCode] = Field(default_factory=list)
    degradations: list[DegradationCode] = Field(default_factory=list)


class RunDebugReportArtifact(RunPublicReportArtifact):
    artifact_ids: list[str] = Field(default_factory=list)
    provider_invocation_ids: list[str] = Field(default_factory=list)
    node_errors: list[NodeError] = Field(default_factory=list)


class RunReportResponse(ContractModel):
    public_report: RunPublicReportArtifact
    debug_report: RunDebugReportArtifact | None = None
    request_id: str = "req_local"


class RunArtifactsResponse(ContractModel):
    run_id: str
    artifacts: list[ArtifactRef]
    request_id: str


def build_run_config_summary(run_id: str, job: Job) -> RunConfigSummary:
    """Project a job's video request into the run-detail config snapshot."""
    request = job.request
    if not isinstance(request, DigitalHumanVideoRequest):
        return RunConfigSummary(run_id=run_id, job_id=job.id)
    return RunConfigSummary(
        run_id=run_id,
        job_id=job.id,
        workflow_template_id=request.workflow_template_id,
        title=request.title,
        script=request.script,
        publish_content=request.publish_content or None,
        voice_id=request.voice.voice_id,
        voice_provider_profile_id=request.voice.provider_profile_id,
        voice_speed=request.voice.speed,
        voice_emotion=request.voice.emotion,
        width=request.output.width,
        height=request.output.height,
        fps=request.output.fps,
        subtitle_enabled=request.subtitle.enabled,
        broll_enabled=request.broll.enabled,
        lipsync_enabled=request.lipsync.enabled,
    )
