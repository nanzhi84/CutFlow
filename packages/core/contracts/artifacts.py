from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from packages.core.contracts.caption_policy import (
    CAPTION_ANCHOR_X,
    CAPTION_BASELINE_Y,
    CAPTION_LINE_HEIGHT_RATIO,
    CAPTION_MAX_WIDTH_RATIO,
)

from packages.core.contracts import (
    ArtifactRef,
    CaseMemory,
    ContractModel,
    DegradationNotice,
    NodeError,
    ScriptVersion,
    SpeechTiming,
    SpeechTokenTiming,
    utcnow,
)


class MaterialCandidate(ContractModel):
    asset_id: str
    score: float = 0
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubtitleStylePlan(ContractModel):
    normal_enabled: bool | None = None
    emphasis_enabled: bool | None = None
    font_id: str | None = None
    emphasis_font_id: str | None = None
    font_size: int | None = None
    emphasis_font_size: int | None = None
    position: dict[str, float] | None = None
    font_weight: int | None = None
    primary_color: str | None = None
    outline_color: str | None = None
    outline: float | None = None
    emphasis_font_weight: int | None = None
    emphasis_primary_color: str | None = None
    emphasis_outline_color: str | None = None
    emphasis_outline: float | None = None


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
    emphasis_font_id: str | None = None


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
    """A script-exact phrase that may become an inline caption run."""

    phrase: str = Field(min_length=1)
    priority: int = Field(50, ge=0, le=100)
    display_mode: Literal["inline", "whole_cue"] = "inline"


class CreativeIntentArtifact(ContractModel):
    """ResolveCreativeIntent 产出的 LLM 创意语义判断。

    只承载 LLM 的低基数语义（hook/beats 的 ``intent`` + 强调短语）；带时间轴的
    Caption Run 由下游确定性规划派生，不存这里。
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
    tokens: list[SpeechTokenTiming] = Field(default_factory=list)
    source: Literal["tts", "asr", "estimated", "tts_subtitle", "forced_alignment"] = "estimated"
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    language: str | None = None


class RawSpeechAlignmentArtifact(ContractModel):
    """Durable TTS-native timing passed between Temporal activities."""

    audio_artifact_id: str
    source: Literal["tts"] = "tts"
    timing: SpeechTiming
    provider_invocation_id: str | None = None


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
    source: Literal["tts", "tts_subtitle", "forced_alignment", "asr", "estimated"]
    units: list[NarrationUnit]
    strict: bool
    warnings: list[str] = Field(default_factory=list)


class NarrationBoundaryPlan(ContractModel):
    """Safe-cut boundary plan produced by NarrationBoundaryPlanning (#135).

    Front-moves the editing-boundary responsibility out of timeline-window planning: the ffmpeg
    silence detection + semantic/audio safe-cut assembly now live in one node right after
    NarrationAlignment. Downstream planning nodes read these frame-quantized windows
    instead of re-detecting pauses.

    ``pause_windows`` is the raw ``detect_silence_windows`` output
    (``{start,end,duration,center}``); TimelineWindowPlanning consumes it as the audio-pause
    input to its coverage/escalation planner, so portrait main-track frame
    boundaries stay identical to before the split. ``safe_cut_boundaries`` /
    ``portrait_slots`` / ``broll_slots`` are frame-quantized base/available windows,
    NOT final authority: the authoritative main-track plan is emitted by
    TimelineWindowPlanning, while these windows describe where cuts may safely land for the
    future comprehensive editing agent (see #136).
    """

    fps: int
    total_frames: int
    source: str
    pause_windows: list[dict[str, float]] = Field(default_factory=list)
    safe_cut_boundaries: list[dict[str, Any]] = Field(default_factory=list)
    portrait_slots: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Base/available portrait windows, NOT final authority.",
    )
    broll_slots: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Base/available B-roll windows, NOT final authority.",
    )
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class TimelineWindowsPlan(ContractModel):
    """Authoritative timeline-window registry emitted by TimelineWindowPlanning.

    ``broll_windows`` are optional placement slots, not suggestions for downstream
    re-planning. A downstream assignment may skip any window, but when it binds a
    candidate to one, the final ``plan.broll`` overlay must keep that window's
    ``start_frame`` / ``end_frame`` unchanged. Materializers verify the candidate
    source span can cover ``length_frames`` and expand the selection; they must not
    add, move, resize, or re-snap these windows.
    """

    fps: int
    total_frames: int
    geometry_policy: dict[str, Any] = Field(default_factory=dict)
    portrait_windows: list[dict[str, Any]] = Field(default_factory=list)
    broll_windows: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Authoritative optional B-roll placement slots.",
    )
    default_assignment: dict[str, Any] = Field(default_factory=dict)
    compile_diagnostics: dict[str, Any] = Field(default_factory=dict)


class WindowRetrievalQuery(ContractModel):
    window_id: str
    retrieval_intent: str


class WindowQueryPlanArtifact(ContractModel):
    window_queries: list[WindowRetrievalQuery] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class ClipEmbeddingRecord(ContractModel):
    clip_embedding_key: str
    asset_id: str
    asset_revision: str
    clip_id: str
    source_start: float
    source_end: float
    source_frames_available: int
    index_namespace: Literal["portrait", "broll"]
    embedding_scope: Literal["clip"] = "clip"
    embedding_input_type: Literal["video_clip", "sampled_frames"] = "video_clip"
    embedding_input_ref: str
    sample_policy: dict[str, Any] = Field(default_factory=dict)
    embedding_id: str
    embedding: list[float] = Field(default_factory=list)
    provider_profile_id: str
    embedding_model: str = "qwen3-vl-embedding"
    embedding_dimension: int = 1024
    normalization: str = "l2"
    instruct: str = "video_clip_retrieval_v1"
    index_version: str = "clip-video-qwen3-v3"

    @model_validator(mode="after")
    def validate_embedding_vector(self) -> "ClipEmbeddingRecord":
        if self.embedding_dimension != 1024:
            raise ValueError("clip embedding dimension must be 1024")
        if len(self.embedding) != self.embedding_dimension:
            raise ValueError("clip embedding vector length must equal embedding_dimension")
        if not all(math.isfinite(float(value)) for value in self.embedding):
            raise ValueError("clip embedding vector must contain only finite values")
        return self


class RetrievedWindowCandidate(ContractModel):
    candidate_id: str
    clip_embedding_key: str
    asset_id: str
    clip_id: str
    source_start: float
    source_end: float
    source_frames_available: int
    required_frames: int
    semantic_similarity: float
    recency_adjustment: float = 0.0
    deterministic_tiebreaker: float = 0.0
    retrieval_score: float
    retrieval_trace: dict[str, Any] = Field(default_factory=dict)


class WindowMaterialRetrievalArtifact(ContractModel):
    candidates_by_window: dict[str, list[RetrievedWindowCandidate]] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class MediaPortraitAssignment(ContractModel):
    window_id: str
    candidate_id: str
    source_mode: str = "lipsynced"
    reason: str = ""


class MediaBrollAssignment(ContractModel):
    window_id: str
    candidate_id: str
    reason: str = ""
    confidence: float = 0.0
    matched_keywords: list[str] = Field(default_factory=list)


class MediaAssignmentPlan(ContractModel):
    engine: Literal["editing_agent_llm", "deterministic_default", "deterministic_fallback"]
    portrait: list[MediaPortraitAssignment] = Field(default_factory=list)
    broll: list[MediaBrollAssignment] = Field(default_factory=list)
    font_id: str | None = None
    bgm_id: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class MediaSelectionAssignmentPlan(ContractModel):
    """Active v2 media-only assignment; post-process choices cannot enter this type."""

    engine: Literal["media_selection_agent_llm", "deterministic_fallback"]
    portrait: list[MediaPortraitAssignment] = Field(default_factory=list)
    broll: list[MediaBrollAssignment] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class MaterialPackArtifact(ContractModel):
    case_id: str
    portrait_candidates: list[MaterialCandidate] = Field(default_factory=list)
    broll_candidates: list[MaterialCandidate] = Field(default_factory=list)
    font_candidates: list[MaterialCandidate] = Field(default_factory=list)
    bgm_candidates: list[MaterialCandidate] = Field(default_factory=list)
    rejected_candidates: list[dict[str, Any]] = Field(default_factory=list)
    reservations: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class PortraitSegment(ContractModel):
    """One frame-aligned portrait main-track segment (#105).

    Strongly-typed replacement for the former ``list[dict[str, Any]]`` portrait
    plan segments. The four ``*_frame`` fields are the authoritative boundaries on
    the fixed 30fps grid and are REQUIRED — a missing frame is an upstream contract
    defect that must fail construction, not be silently re-derived from seconds. The
    ``*_sec`` fields are derived display/debug values retained because the jianying
    draft builder and the selection-ledger reader still read them. The field set
    mirrors ``packages.production.pipeline.nodes.timeline_window_planning._segment_payload``
    exactly; ``extra="forbid"`` makes any drift fail loudly at construction.
    """

    segment_id: str
    asset_id: str | None = None
    clip_id: str | None = None
    start_sec: float
    end_sec: float
    source_start: float
    source_end: float
    role: str = "main"
    source_mode: str
    boundary_source: str | None = None
    boundary_reason: str | None = None
    unit_ids: list[str] = Field(default_factory=list)
    slot_phase: str
    recently_used_material: bool = False
    timeline_start_frame: int
    timeline_end_frame: int
    source_start_frame: int
    source_end_frame: int


class PortraitPlanArtifact(ContractModel):
    fps: int = 30
    total_duration: float = 0
    asset_id: str | None = None
    duration_sec: float = 0
    segments: list[PortraitSegment] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class BrollOverlay(ContractModel):
    overlay_id: str
    window_id: str | None = None
    asset_id: str
    clip_id: str | None = None
    timeline_start: float
    timeline_end: float
    source_start: float
    source_end: float
    # Frame-aligned authoritative B-roll boundaries. Optional on the contract because
    # (a) the legacy reader ``broll_overlays_from_plan`` has no fps to derive them and
    # (b) historical seconds-only overlays may have no portrait-cut grid. In
    # digital_human_v2 these fields must match the authoritative
    # ``TimelineWindowsPlan.broll_windows`` slot; TimelineAssemblyValidation fail-fasts
    # when they are missing or drift. ``pad_start``/``pad_end`` remain for legacy snapped
    # plans, but the v2 materializer no longer uses them to resize B-roll windows.
    timeline_start_frame: int | None = None
    timeline_end_frame: int | None = None
    source_start_frame: int | None = None
    source_end_frame: int | None = None
    pad_start: float = 0.0
    pad_end: float = 0.0
    placement: Literal["fullscreen", "pip_fixed"] | None = None
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


class StylePlanArtifact(ContractModel):
    subtitle: SubtitleStylePlan
    bgm: BgmPlan | None = None
    font: FontPlan | None = None
    font_asset_id: str | None = None
    emphasis_font_asset_id: str | None = None
    bgm_asset_id: str | None = None


class CaptionFrameSpan(ContractModel):
    """Half-open frame range used to separate speech truth from display policy."""

    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_span(self) -> "CaptionFrameSpan":
        if self.end_frame <= self.start_frame:
            raise ValueError("caption frame span end must be greater than start")
        return self


class BgmRepairTraceItem(ContractModel):
    attempt: int = Field(ge=0)
    error_count: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)


class MediaSelectionChoiceDiagnostic(ContractModel):
    slot_id: str
    candidate_id: str
    reason: str = ""


class MediaSelectionCandidateCounts(ContractModel):
    portrait: int = Field(ge=0)
    broll: int = Field(ge=0)


class MediaSelectionAgentDiagnosticsArtifact(ContractModel):
    """Typed active-v2 media-selection diagnostics.

    ``repair_trace`` and geometry drops remain extensible nested audit payloads,
    while every stable top-level field is required and type-checked before the
    artifact crosses the node boundary.
    """

    mode: str
    instruction: str = ""
    analysis: str = ""
    repair_trace: list[dict[str, Any]] = Field(default_factory=list)
    portrait_choices: list[MediaSelectionChoiceDiagnostic] = Field(default_factory=list)
    broll_choices: list[MediaSelectionChoiceDiagnostic] = Field(default_factory=list)
    broll_drops: list[dict[str, Any]] = Field(default_factory=list)
    shortlist_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    retrieval_topk_by_window: dict[str, list[str]] = Field(default_factory=dict)
    fallback_used: bool = False
    fallback_reason: str | None = None
    prompt_candidate_domains: dict[str, Any] = Field(default_factory=dict)
    candidate_counts: MediaSelectionCandidateCounts


class BgmAgentDiagnosticsArtifact(ContractModel):
    """BgmAgentPlanning diagnostics; caption choices are intentionally absent."""

    policy_version: Literal["bgm_agent_v1"] = "bgm_agent_v1"
    planned: bool
    reason: str = ""
    bgm_id: str | None = None
    asset_id: str | None = None
    segment_id: str | None = None
    analysis: str = ""
    repair_trace: list[BgmRepairTraceItem] = Field(default_factory=list)
    candidate_count: int = Field(0, ge=0)
    provider_invocation_ids: list[str] = Field(default_factory=list)


class CaptionRun(ContractModel):
    run_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    role: Literal["normal", "emphasis"]
    hint_id: str | None = None
    token_ids: list[str] = Field(default_factory=list)
    char_span: tuple[int, int]
    enter_frame: int = Field(ge=0)
    exit_frame: int = Field(gt=0)
    effect_id: Literal["none", "soft_in", "pop"] = "none"
    font_asset_id: str | None = None
    advance_px: float = Field(ge=0.0)
    baseline_offset_px: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_run(self) -> "CaptionRun":
        start, end = self.char_span
        if start < 0 or end <= start:
            raise ValueError("caption run char_span must be a positive half-open range")
        if self.exit_frame <= self.enter_frame:
            raise ValueError("caption run exit_frame must be greater than enter_frame")
        allowed = {"normal": {"none", "soft_in"}, "emphasis": {"none", "pop"}}
        if self.effect_id not in allowed[self.role]:
            raise ValueError(f"{self.role} caption run cannot use {self.effect_id}")
        if self.role == "emphasis" and not self.hint_id:
            raise ValueError("emphasis caption run requires hint_id")
        if self.role == "normal" and self.hint_id is not None:
            raise ValueError("normal caption run cannot carry hint_id")
        return self


class CaptionLine(ContractModel):
    runs: list[CaptionRun] = Field(min_length=1)
    advance_px: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_line(self) -> "CaptionLine":
        if abs(sum(run.advance_px for run in self.runs) - self.advance_px) > 0.51:
            raise ValueError("caption line advance must equal the sum of run advances")
        return self


class CaptionCue(ContractModel):
    cue_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    spoken_span: CaptionFrameSpan
    display_span: CaptionFrameSpan
    source_unit_ids: list[str] = Field(min_length=1)
    lines: list[CaptionLine] = Field(min_length=1, max_length=3)
    omitted_break_whitespace: list[tuple[int, int]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_cue(self) -> "CaptionCue":
        if (self.start_frame, self.end_frame) != (
            self.display_span.start_frame,
            self.display_span.end_frame,
        ):
            raise ValueError("caption cue start/end must mirror display_span")
        flattened = [run for line in self.lines for run in line.runs]
        if not flattened:
            raise ValueError("caption cue requires at least one run")
        previous_end = 0
        reconstructed: list[str] = []
        omitted = {tuple(span) for span in self.omitted_break_whitespace}
        for start, end in omitted:
            if start < 0 or end <= start or end > len(self.text):
                raise ValueError("omitted caption whitespace span is out of bounds")
            if not self.text[start:end].isspace():
                raise ValueError("only break whitespace may be omitted from caption runs")
        token_ids: set[str] = set()
        for run in flattened:
            start, end = run.char_span
            if start < previous_end:
                raise ValueError("caption run char spans must be ordered and non-overlapping")
            if end > len(self.text):
                raise ValueError("caption run char span exceeds cue text")
            gap = self.text[previous_end:start]
            if gap and (previous_end, start) not in omitted:
                raise ValueError("caption run char spans leave an unexplained text gap")
            reconstructed.append(gap)
            if self.text[start:end] != run.text:
                raise ValueError("caption run text must match the cue substring")
            reconstructed.append(run.text)
            if run.enter_frame < self.start_frame or run.exit_frame > self.end_frame:
                raise ValueError("caption run timing must stay inside its cue")
            duplicates = token_ids.intersection(run.token_ids)
            if duplicates:
                raise ValueError("caption token ids must be owned by one run")
            token_ids.update(run.token_ids)
            previous_end = end
        trailing = self.text[previous_end:]
        if trailing and (previous_end, len(self.text)) not in omitted:
            raise ValueError("caption run char spans leave an unexplained trailing gap")
        reconstructed.append(trailing)
        if "".join(reconstructed) != self.text:
            raise ValueError("caption runs must reconstruct the cue text")
        return self


class CaptionBand(ContractModel):
    anchor_x: float = Field(CAPTION_ANCHOR_X, ge=0.0, le=1.0)
    baseline_y: float = Field(CAPTION_BASELINE_Y, ge=0.0, le=1.0)
    line_height_ratio: float = Field(CAPTION_LINE_HEIGHT_RATIO, gt=0.0)
    text_align: Literal["center"] = "center"
    max_width_ratio: float = Field(CAPTION_MAX_WIDTH_RATIO, gt=0.0, le=1.0)


class CaptionTokenFallback(ContractModel):
    reason: Literal["token_unmatched"] = "token_unmatched"
    hint_ids: list[str] = Field(min_length=1)
    phrase: str


class CaptionLayoutFallback(ContractModel):
    reason: Literal["emphasis_unbreakable"] = "emphasis_unbreakable"
    hint_ids: list[str] = Field(min_length=1)
    source_unit_ids: list[str] = Field(min_length=1)


class CaptionUnitFallback(ContractModel):
    reason: Literal["narration_unit_unmatched", "narration_unit_timing_invalid"]
    source_unit_ids: list[str] = Field(min_length=1)
    text: str


class CaptionCompositionDiagnostics(ContractModel):
    timing_source: Literal["native", "asr_anchored", "interpolated"] = "interpolated"
    font_metrics_source: Literal["hmtx", "eaw_fallback"] = "hmtx"
    emphasis_font_metrics_source: Literal["hmtx", "eaw_fallback"] = "hmtx"
    font_horizontal_overhang_px: dict[str, float] = Field(default_factory=dict)
    font_horizontal_left_overhang_px: dict[str, float] = Field(default_factory=dict)
    font_horizontal_right_overhang_px: dict[str, float] = Field(default_factory=dict)
    merged_units: int = Field(0, ge=0)
    split_cues: int = Field(0, ge=0)
    units_unmatched: int = Field(0, ge=0)
    hints_total: int = Field(0, ge=0)
    hints_applied: int = Field(0, ge=0)
    hints_unmatched: int = Field(0, ge=0)
    hints_token_unmatched: int = Field(0, ge=0)
    hints_overlapped: int = Field(0, ge=0)
    hints_unbreakable: int = Field(0, ge=0)
    fallbacks: list[CaptionTokenFallback | CaptionLayoutFallback | CaptionUnitFallback] = Field(
        default_factory=list
    )


class CaptionCompositionPlanArtifact(ContractModel):
    policy_version: Literal["caption_composition_v1"] = "caption_composition_v1"
    fps: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    normal_enabled: bool
    emphasis_enabled: bool
    band: CaptionBand
    normal_font_asset_id: str | None = None
    emphasis_font_asset_id: str | None = None
    normal_font_size: int = Field(gt=0)
    emphasis_font_size: int = Field(gt=0)
    cues: list[CaptionCue] = Field(default_factory=list)
    diagnostics: CaptionCompositionDiagnostics = Field(
        default_factory=CaptionCompositionDiagnostics
    )

    @model_validator(mode="after")
    def validate_plan(self) -> "CaptionCompositionPlanArtifact":
        if self.emphasis_enabled and not self.normal_enabled:
            raise ValueError("emphasis captions require the normal caption band")
        if not self.normal_enabled and self.cues:
            raise ValueError("disabled normal captions cannot contain cues")
        if self.normal_enabled and not self.normal_font_asset_id:
            raise ValueError("enabled captions require normal_font_asset_id")
        if self.emphasis_enabled and not self.emphasis_font_asset_id:
            raise ValueError("enabled emphasis requires emphasis_font_asset_id")
        cue_ids = [cue.cue_id for cue in self.cues]
        if len(set(cue_ids)) != len(cue_ids):
            raise ValueError("caption cue ids must be unique")
        claimed_tokens: set[str] = set()
        for cue in self.cues:
            for line in cue.lines:
                for run in line.runs:
                    if run.role == "emphasis" and not self.emphasis_enabled:
                        raise ValueError("disabled emphasis cannot contain emphasis runs")
                    duplicates = claimed_tokens.intersection(run.token_ids)
                    if duplicates:
                        raise ValueError("caption token ids must be globally unique")
                    claimed_tokens.update(run.token_ids)
        return self


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
    placement: Literal["fullscreen", "pip_fixed"] | None = None


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
