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
    utcnow,
)


class MaterialCandidate(ContractModel):
    asset_id: str
    score: float = 0
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubtitleStylePlan(ContractModel):
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


class TimelineValidationReport(ContractModel):
    valid: bool
    errors: list[NodeError] = Field(default_factory=list)
    warnings: list[DegradationNotice] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


class CaseContextArtifact(ContractModel):
    case_id: str
    case_profile: dict[str, Any] = Field(default_factory=dict)
    active_memories: list[CaseMemory] = Field(default_factory=list)
    recent_script_versions: list[ScriptVersion] = Field(default_factory=list)
    performance_summary: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utcnow)


class EmphasisHint(ContractModel):
    """LLM 标记的整句强调关键短语（花字地基）。

    ``phrase`` 是脚本里值得做花字/整句强调的关键短语（取自原话，便于确定性子串
    定位到旁白句）。StylePlanning 把它匹配到旁白句、换算成带时间轴的 OverlayEvent。
    刻意用短语而非 beat 序号：beat 是 LLM 转述、与旁白文本不可靠对应；短语是原话、
    子串匹配确定可复现，也更贴合未来逐词花字。
    """

    phrase: str


class CreativeIntentArtifact(ContractModel):
    """ResolveCreativeIntent 产出的 LLM 创意语义判断。

    只承载 LLM 的低基数语义（hook/beats 的 ``intent`` + 强调短语）；带时间轴的字幕
    事件等 render 结果由下游确定性节点（StylePlanning）派生，不存这里。
    """

    intent: dict[str, Any] | None = None
    emphasis: list[EmphasisHint] = Field(default_factory=list)


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
    # ``overlays`` is the single canonical B-roll insert structure (#104). The
    # legacy dict ``segments`` double-write was removed; readers go through
    # ``packages.production._broll_overlays.broll_overlays_from_plan`` which still
    # derives overlays from any pre-#104 persisted ``segments``.
    enabled: bool
    overlays: list[BrollOverlay] = Field(default_factory=list)
    skipped_reason: str | None = None


class OverlayEvent(ContractModel):
    """StylePlanning 确定性派生的带时间轴字幕浮层事件（整句强调 / 花字地基）。

    由 ``CreativeIntentArtifact.emphasis`` 的关键短语匹配旁白句换算而来，渲染层把它
    叠成一条独立样式的字幕。``text`` 是要强调的短语本身（非整句，避免与底部正文重复）。
    本期只有"强调"一种样式，故不带 style 判别字段；未来花字做多样式分流时再连同渲染层
    的消费一起加，避免现在留一个写了不读的死字段。
    """

    start: float
    end: float
    text: str


class StylePlanArtifact(ContractModel):
    subtitle: SubtitleStylePlan
    bgm: BgmPlan | None = None
    font: FontPlan | None = None
    font_asset_id: str | None = None
    bgm_asset_id: str | None = None
    overlay_events: list[OverlayEvent] = Field(default_factory=list)


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
