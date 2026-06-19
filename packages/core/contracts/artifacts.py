from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    CaseMemory,
    ContractModel,
    DegradationNotice,
    EditorHandoffPackageArtifact,
    JianyingDraftPackageArtifact,
    MediaInfo,
    NodeError,
    RunDebugReportArtifact,
    RunPublicReportArtifact,
    ScriptVersion,
    VideoVersion,
    utcnow,
)


class MaterialCandidate(ContractModel):
    asset_id: str
    score: float = 0
    reason: str = ""
    reservation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubtitleStylePlan(ContractModel):
    enabled: bool = True
    style_preset: str = "douyin"
    font_id: str | None = None
    font_size: int | None = None
    position: dict[str, float] | None = None


class BgmPlan(ContractModel):
    enabled: bool = True
    asset_id: str | None = None
    segment_id: str | None = None
    source_start: float | None = None
    source_end: float | None = None
    duration: float | None = None
    mood: str = ""
    scene_fit: list[str] = Field(default_factory=list)
    reason: str = ""
    volume: float = 0.25
    auto_mix: bool = True


class FontPlan(ContractModel):
    font_id: str | None = None
    fallback_family: str = "sans"
    size: int | None = None


class TimelineValidationReport(ContractModel):
    valid: bool
    errors: list[NodeError] = Field(default_factory=list)
    warnings: list[DegradationNotice] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


class UploadedFileArtifact(ContractModel):
    upload_session_id: str | None = None
    filename: str
    content_type: str
    size_bytes: int
    object_uri: str | None = None
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImportMappingArtifact(ContractModel):
    import_batch_id: str | None = None
    import_type: str
    external_to_internal_ids: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaseContextArtifact(ContractModel):
    case_id: str
    case_profile: dict[str, Any] = Field(default_factory=dict)
    active_memories: list[CaseMemory] = Field(default_factory=list)
    recent_script_versions: list[ScriptVersion] = Field(default_factory=list)
    recent_video_versions: list[VideoVersion] = Field(default_factory=list)
    performance_summary: dict[str, Any] = Field(default_factory=dict)
    negative_lessons: list[CaseMemory] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)


class PerformanceAnalysisArtifact(ContractModel):
    case_id: str
    observations: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)


class ScriptStrategyArtifact(ContractModel):
    case_id: str
    strategy_points: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    prompt_guidance: str = ""


class ValidatedProductionSpecArtifact(ContractModel):
    request_id: str
    case_id: str
    workflow_template_id: str
    workflow_template_version: str
    validation_errors: list[NodeError] = Field(default_factory=list)
    validation_warnings: list[DegradationNotice] = Field(default_factory=list)
    normalized_request_artifact_id: str | None = None


class CreativeIntentArtifact(ContractModel):
    scene_type: Literal["hard_ad", "ip_persona"] = "hard_ad"
    style_hint: str = ""
    density: str = "medium"
    closing_cta: str = ""
    intent: dict[str, Any] | None = None
    cover_focus: dict[str, Any] = Field(default_factory=dict)
    overlay_events: list[dict[str, Any]] = Field(default_factory=list)
    script_features_hint: dict[str, Any] = Field(default_factory=dict)


class RawAlignmentArtifact(ContractModel):
    provider_invocation_id: str | None = None
    format: Literal["json", "srt", "textgrid", "provider_raw"]
    source_artifact_id: str | None = None
    segments: list[dict[str, Any]] = Field(default_factory=list)


class AlignmentSegment(ContractModel):
    text: str
    start_sec: float
    end_sec: float
    word_confidence: float | None = None


class AlignmentArtifact(ContractModel):
    audio_artifact_id: str
    segments: list[AlignmentSegment]
    language: str | None = None


class NarrationUnit(ContractModel):
    unit_id: str
    text: str
    start: float
    end: float
    confidence: float
    # Boundary-planning fields (additive, all defaulted so existing callers are
    # unaffected). The editing-agent boundary planner reads these to decide where
    # portrait cuts may land; the narration splitter populates them.
    duration: float | None = None
    intent: str = "explain"
    pause_after_ms: int = 0
    hard_end: bool = False
    boundary_score: float = 0.0
    portrait_cut_allowed: bool = False
    broll_overlay_allowed: bool = False
    boundary_reason: str = ""


class NarrationUnitsArtifact(ContractModel):
    source: Literal["tts_subtitle", "forced_alignment", "asr", "estimated"]
    units: list[NarrationUnit]
    strict: bool
    warnings: list[str] = Field(default_factory=list)


class MaterialPackArtifact(ContractModel):
    case_id: str
    portrait_candidates: list[MaterialCandidate] = Field(default_factory=list)
    broll_candidates: list[MaterialCandidate] = Field(default_factory=list)
    font_candidates: list[MaterialCandidate] = Field(default_factory=list)
    bgm_candidates: list[MaterialCandidate] = Field(default_factory=list)
    reservations: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class PortraitPlanArtifact(ContractModel):
    fps: int = 30
    total_duration: float = 0
    asset_id: str | None = None
    duration_sec: float = 0
    segments: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class BrollOverlay(ContractModel):
    overlay_id: str
    asset_id: str
    clip_id: str | None = None
    timeline_start: float
    timeline_end: float
    source_start: float
    source_end: float
    reason: str
    confidence: float
    matched_keywords: list[str] = Field(default_factory=list)
    scene_name: str | None = None
    # Diversity cluster (scene_type/narrative_role) carried so FinalizeRunReport
    # can persist it into the selection ledger and cluster-level recency demotion
    # can fire on the next run. Not part of the public OpenAPI surface.
    diversity_key: str | None = None


class BrollPlanArtifact(ContractModel):
    enabled: bool
    segments: list[dict[str, Any]] = Field(default_factory=list)
    overlays: list[BrollOverlay] = Field(default_factory=list)
    skipped_reason: str | None = None


class StylePlanArtifact(ContractModel):
    subtitle: SubtitleStylePlan
    bgm: BgmPlan | None = None
    font: FontPlan | None = None
    font_asset_id: str | None = None
    bgm_asset_id: str | None = None
    subtitle_enabled: bool = True
    selection_reservation_ids: list[str] = Field(default_factory=list)


class TimelineTrackSegment(ContractModel):
    track_id: str
    segment_id: str
    asset_ref: ArtifactRef
    timeline_start_frame: int
    timeline_end_frame: int
    source_start_frame: int | None = None
    source_end_frame: int | None = None


class TimelinePlanArtifact(ContractModel):
    fps: int = 30
    total_frames: int
    tracks: list[TimelineTrackSegment]
    validation: TimelineValidationReport


class RenderPlanArtifact(ContractModel):
    timeline_artifact_id: str
    render_size: tuple[int, int]
    fps: int
    output_format: str = "mp4"
    tracks: list[TimelineTrackSegment]


class LipSyncReportArtifact(ContractModel):
    provider_invocation_id: str | None = None
    provider_profile_id: str | None = None
    skipped: bool = False
    skipped_reason: str | None = None
    input_video_artifact_id: str
    input_audio_artifact_id: str
    output_video_artifact_id: str
    fallback_from: str | None = None
    fallback_to: str | None = None
    fallback_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class PublishPackageArtifact(ContractModel):
    publish_package_id: str
    manifest_version: str
    video_artifact_id: str
    cover_artifact_id: str | None = None
    title: str
    description: str
    platform_targets: list[str] = Field(default_factory=list)


class ProviderRawRequestArtifact(ContractModel):
    provider_invocation_id: str
    redaction_policy_version: str
    body_artifact_uri: str
    content_type: str


class ProviderRawResponseArtifact(ContractModel):
    provider_invocation_id: str
    redaction_policy_version: str
    body_artifact_uri: str
    content_type: str
    status_code: int | None = None


class _UriOnlyArtifactEnvelope(BaseModel):
    uri: str
    sha256: str
    media_info: MediaInfo


class ArtifactSchemaRegistry:
    def __init__(self, models: dict[tuple[ArtifactKind, str], type[ContractModel]]) -> None:
        self._models = models

    @classmethod
    def default(cls) -> "ArtifactSchemaRegistry":
        entries: dict[ArtifactKind, type[ContractModel]] = {
            ArtifactKind.uploaded_file: UploadedFileArtifact,
            ArtifactKind.validated_production_spec: ValidatedProductionSpecArtifact,
            ArtifactKind.case_context: CaseContextArtifact,
            ArtifactKind.case_performance_analysis: PerformanceAnalysisArtifact,
            ArtifactKind.script_strategy: ScriptStrategyArtifact,
            ArtifactKind.creative_intent: CreativeIntentArtifact,
            ArtifactKind.audio_alignment_raw: RawAlignmentArtifact,
            ArtifactKind.audio_alignment: AlignmentArtifact,
            ArtifactKind.narration_units: NarrationUnitsArtifact,
            ArtifactKind.material_pack: MaterialPackArtifact,
            ArtifactKind.portrait_plan: PortraitPlanArtifact,
            ArtifactKind.broll_plan: BrollPlanArtifact,
            ArtifactKind.style_plan: StylePlanArtifact,
            ArtifactKind.timeline_plan: TimelinePlanArtifact,
            ArtifactKind.render_plan: RenderPlanArtifact,
            ArtifactKind.lipsync_report: LipSyncReportArtifact,
            ArtifactKind.editor_handoff_package: EditorHandoffPackageArtifact,
            ArtifactKind.jianying_draft_package: JianyingDraftPackageArtifact,
            ArtifactKind.publish_package: PublishPackageArtifact,
            ArtifactKind.run_public_report: RunPublicReportArtifact,
            ArtifactKind.run_debug_report: RunDebugReportArtifact,
            ArtifactKind.provider_raw_request: ProviderRawRequestArtifact,
            ArtifactKind.provider_raw_response: ProviderRawResponseArtifact,
            ArtifactKind.import_mapping: ImportMappingArtifact,
        }
        return cls({(kind, "v1"): model for kind, model in entries.items()})

    @property
    def uri_only_kinds(self) -> frozenset[ArtifactKind]:
        return frozenset(
            {
                ArtifactKind.audio_tts,
                ArtifactKind.video_portrait_track,
                ArtifactKind.video_lipsync,
                ArtifactKind.video_rendered,
                ArtifactKind.video_final,
                ArtifactKind.video_finished,
                ArtifactKind.subtitle_ass,
                ArtifactKind.cover_image,
            }
        )

    def model_for(self, kind: ArtifactKind, schema_version: str) -> type[ContractModel]:
        version = "v1" if schema_version.endswith(".v1") else schema_version
        return self._models[(kind, version)]

    def validate_artifact(self, artifact: Artifact) -> Artifact:
        if artifact.kind in self.uri_only_kinds:
            _UriOnlyArtifactEnvelope.model_validate(artifact.model_dump())
            return artifact
        model = self.model_for(artifact.kind, artifact.schema_version)
        model.model_validate(artifact.payload)
        return artifact
