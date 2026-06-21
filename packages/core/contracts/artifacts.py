from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from packages.core.contracts import (
    ArtifactRef,
    CaseMemory,
    ContractModel,
    DegradationNotice,
    NodeError,
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
    section_type: str = ""
    section_label: str = ""
    repeat_group: str = ""
    loopable: bool = False
    energy_profile: str = ""
    mood: str = ""
    scene_fit: list[str] = Field(default_factory=list)
    script_fit: list[str] = Field(default_factory=list)
    avoid_script: list[str] = Field(default_factory=list)
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


class CaseContextArtifact(ContractModel):
    case_id: str
    case_profile: dict[str, Any] = Field(default_factory=dict)
    active_memories: list[CaseMemory] = Field(default_factory=list)
    recent_script_versions: list[ScriptVersion] = Field(default_factory=list)
    recent_video_versions: list[VideoVersion] = Field(default_factory=list)
    performance_summary: dict[str, Any] = Field(default_factory=dict)
    negative_lessons: list[CaseMemory] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)


class CreativeIntentArtifact(ContractModel):
    scene_type: Literal["hard_ad", "ip_persona"] = "hard_ad"
    style_hint: str = ""
    density: str = "medium"
    closing_cta: str = ""
    intent: dict[str, Any] | None = None
    cover_focus: dict[str, Any] = Field(default_factory=dict)
    overlay_events: list[dict[str, Any]] = Field(default_factory=list)
    script_features_hint: dict[str, Any] = Field(default_factory=dict)


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
    pad_start: float = 0.0
    pad_end: float = 0.0


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
