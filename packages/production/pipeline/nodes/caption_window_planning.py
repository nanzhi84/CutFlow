"""CaptionWindowPlanning: deterministic caption timing and pixel-safe options."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from packages.core.contracts import ArtifactKind, DegradationNotice, NodeStatus, WarningCode
from packages.core.contracts.artifacts import CaptionWindowsPlanArtifact
from packages.core.workflow import NodeOutput
from packages.media.annotation.sensors.frames import extract_frames_for_times
from packages.production.pipeline._caption_visual_safety import (
    EMPHASIS_MIN_EVENTS,
    EMPHASIS_TIER2_BUSY_MAX,
    EMPHASIS_TIER2_SCENE_TEXT_MAX,
    count_face_blocked,
    measure_option_candidates,
    sample_frame_indices,
    select_best_face_clear_option,
    select_options_at_thresholds,
)
from packages.production.pipeline._caption_window_planner import (
    build_caption_option_candidates,
    build_emphasis_windows,
    compile_normal_windows,
    finalize_safe_caption_options,
    normal_safe_rect,
    timeline_cut_frames,
)
from packages.production.pipeline._font_metrics import load_font_metrics, make_text_measurer
from packages.production.pipeline._fonts import resolve_font_asset
from packages.production.pipeline._huazi_candidates import normal_caption_top_y
from packages.production.pipeline._materialize import (
    _subtitle_colors,
    _subtitle_font_size,
    _subtitle_position,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline._subtitles import (
    _ASS_MARGIN_L,
    _ASS_MARGIN_R,
    ass_font_size,
)
from packages.production.pipeline.nodes._creative_intent import load_creative_intent


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    rendered = state.require(ArtifactKind.video_rendered)
    timeline_artifact = state.require(ArtifactKind.plan_timeline)
    narration_artifact = state.require(ArtifactKind.narration_units)
    alignment_artifact = state.artifacts.get(ArtifactKind.audio_alignment)
    timeline = timeline_artifact.payload or {}
    narration = narration_artifact.payload or {}
    units = [item for item in (narration.get("units") or []) if isinstance(item, dict)]
    alignment = alignment_artifact.payload or {} if alignment_artifact is not None else {}
    tokens = [item for item in (alignment.get("tokens") or []) if isinstance(item, dict)]
    creative_intent = load_creative_intent(state)

    width = int(state.request.output.width)
    height = int(state.request.output.height)
    fps = max(1, int(timeline.get("fps") or state.request.output.fps))
    total_frames = max(0, int(timeline.get("total_frames") or 0))
    normal_enabled = bool(state.request.subtitle.enabled and state.request.subtitle.normal_enabled)
    emphasis_enabled = bool(
        state.request.subtitle.enabled and state.request.subtitle.emphasis_enabled
    )

    warnings: list[WarningCode] = []
    degradations: list[DegradationNotice] = []
    diagnostics: dict = {
        "merged_units": 0,
        "split_cues": 0,
        "font_metrics_source": "eaw_fallback",
        "sampled_frames": 0,
        "generated_anchor_candidates": 0,
        "rejected_anchor_candidates": 0,
        "visual_analysis_failed": False,
        "emphasis_candidates": 0,
        "events_crossing_cuts_dropped": 0,
        "events_without_options": 0,
        "rejected_face": 0,
        "rejected_scene_text": 0,
        "rejected_busy": 0,
        "unavailable_detectors": [],
        "safe_anchor_candidates": 0,
        "anchors_pruned_by_cap": 0,
        "options_pruned_by_cap": 0,
        "token_matched": 0,
        "char_fallback": 0,
        "emphasis_hold_extended": 0,
        "emphasis_hold_below_min": 0,
    }

    with tempfile.TemporaryDirectory(prefix="cutagent-caption-window-") as directory:
        temp_dir = Path(directory)
        normal_font_id = state.request.subtitle.font_id
        # v3 deliberately uses one resolved coarse-serif font for all three levels.
        resolved_font = None
        normal_metrics = None
        normal_unresolved_font_id = None
        if (normal_enabled or emphasis_enabled) and normal_font_id:
            resolved_font, normal_unresolved_font_id = resolve_font_asset(
                font_asset_id=normal_font_id,
                runtime_dir=temp_dir / "fonts",
                source_artifact_for_asset=ctx.source_artifact_for_asset,
                artifact_path=ctx.artifact_path,
                media_assets=ctx.repository.media_assets,
            )
            if normal_unresolved_font_id:
                _append_degradation(
                    ctx,
                    warnings,
                    degradations,
                    WarningCode.font_resolution_failed,
                    f"指定字幕字体（{normal_unresolved_font_id}）文件无法加载，字幕窗口已按估算字宽规划。",
                )
            normal_metrics = (
                load_font_metrics(resolved_font.source_path) if resolved_font else None
            )
            if resolved_font is not None and normal_metrics is None:
                _append_degradation(
                    ctx,
                    warnings,
                    degradations,
                    WarningCode.font_metrics_fallback,
                    "无法读取所选字体度量，字幕窗口已按估算宽度规划。",
                )
        requested_font_size = _subtitle_font_size(
            state.request.subtitle.style_preset,
            state.request.subtitle.font_size,
        )
        final_ass_font_size = ass_font_size(requested_font_size, height=height)
        measure, metrics_source = make_text_measurer(
            normal_metrics, float(final_ass_font_size)
        )
        cut_frames = timeline_cut_frames(timeline, total_frames)
        normal_windows, normal_diagnostics = compile_normal_windows(
            units=units,
            resolution=(width, height),
            fps=fps,
            total_frames=total_frames,
            margin_l=_ASS_MARGIN_L,
            margin_r=_ASS_MARGIN_R,
            measure=measure,
            metrics_source=metrics_source,
            enabled=normal_enabled,
            tokens=tokens,
            cut_frames=cut_frames,
        )
        diagnostics.update(normal_diagnostics)

        position = _subtitle_position(
            state.request.subtitle.style_preset,
            state.request.subtitle.position,
        )
        position_y = float(position.get("y", 0.84)) if isinstance(position, dict) else 0.84
        caption_top_y = (
            normal_caption_top_y(
                position_y=position_y,
                font_size=final_ass_font_size,
                canvas_height=height,
            )
            if normal_enabled
            else 1.0
        )
        safe_rect = (
            normal_safe_rect(
                width=width,
                position_y=position_y,
                top_y=caption_top_y,
                margin_l=_ASS_MARGIN_L,
                margin_r=_ASS_MARGIN_R,
            )
            if normal_enabled
            else None
        )

        (
            emphasis_windows,
            emphasis_count,
            cut_drops,
            emphasis_token_matched,
            emphasis_char_fallback,
            emphasis_hold_extended,
            emphasis_hold_below_min,
        ) = build_emphasis_windows(
            emphasis=creative_intent.emphasis if emphasis_enabled else [],
            units=units,
            fps=fps,
            total_frames=total_frames,
            cut_frames=cut_frames,
            resolution=(width, height),
            normal_caption_top_y=caption_top_y,
            tokens=tokens,
        )
        diagnostics["emphasis_candidates"] = emphasis_count
        diagnostics["events_crossing_cuts_dropped"] = cut_drops
        diagnostics["token_matched"] += emphasis_token_matched
        diagnostics["char_fallback"] += emphasis_char_fallback
        diagnostics["emphasis_hold_extended"] = emphasis_hold_extended
        diagnostics["emphasis_hold_below_min"] = emphasis_hold_below_min

        emphasis_summary: EmphasisAnalysisSummary | None = None
        if emphasis_enabled:
            final_ass_emphasis_font_size = final_ass_font_size
            emphasis_measure, _emphasis_metrics_source = make_text_measurer(
                normal_metrics,
                float(final_ass_emphasis_font_size),
            )
            colors = _subtitle_colors(state.request.subtitle.style_preset)
            emphasis_summary = _analyze_emphasis_windows(
                video_path=str(ctx.artifact_path(rendered)),
                temp_dir=temp_dir / "frames",
                fps=fps,
                windows=emphasis_windows,
                diagnostics=diagnostics,
                width=width,
                height=height,
                measure=emphasis_measure,
                font_size=float(final_ass_emphasis_font_size),
                outline=float(colors.get("emphasis_outline") or 5.0),
                shadow=1.0,
                normal_safe_rect=safe_rect,
            )

    if emphasis_summary is not None and (
        emphasis_summary.relaxed_tier2_events or emphasis_summary.relaxed_tier3_events
    ):
        notice = degradation_notice(
            WarningCode.caption_emphasis_relaxed_safety,
            "为达到花字下限，部分事件放宽了场景文字/繁忙区安全阈值（人脸红线不变）。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        ).model_copy(
            update={
                "details": {
                    "events_with_options": emphasis_summary.events_with_options,
                    "tier1_events": emphasis_summary.tier1_events,
                    "relaxed_tier2_events": emphasis_summary.relaxed_tier2_events,
                    "relaxed_tier3_events": emphasis_summary.relaxed_tier3_events,
                    "floor": emphasis_summary.floor,
                    "tier2_thresholds": {
                        "scene_text_overlap_max": EMPHASIS_TIER2_SCENE_TEXT_MAX,
                        "busy_score_max": EMPHASIS_TIER2_BUSY_MAX,
                    },
                }
            }
        )
        warnings.append(WarningCode.caption_emphasis_relaxed_safety)
        degradations.append(notice)

    if (
        emphasis_summary is not None
        and emphasis_enabled
        and len(emphasis_windows) > 0
        and emphasis_summary.events_with_options < EMPHASIS_MIN_EVENTS
    ):
        notice = degradation_notice(
            WarningCode.caption_emphasis_below_floor,
            f"花字数量未达下限（{emphasis_summary.events_with_options}/"
            f"{EMPHASIS_MIN_EVENTS}）；已尽可能放宽后仍无法安全放置更多花字。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        ).model_copy(
            update={
                "details": {
                    "events_with_options": emphasis_summary.events_with_options,
                    "floor": EMPHASIS_MIN_EVENTS,
                    "analyzable_events": emphasis_summary.analyzable_events,
                    "candidate_events": len(emphasis_windows),
                    "death_causes": emphasis_summary.death_causes,
                }
            }
        )
        warnings.append(WarningCode.caption_emphasis_below_floor)
        degradations.append(notice)

    if diagnostics["visual_analysis_failed"]:
        details = {
            "unavailable_detectors": list(diagnostics["unavailable_detectors"]),
            "events_without_options": diagnostics["events_without_options"],
        }
        notice = degradation_notice(
            WarningCode.caption_visual_analysis_failed,
            "字幕视觉安全分析未完整完成；无法证明安全的花字事件已取消。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        ).model_copy(update={"details": details})
        warnings.append(WarningCode.caption_visual_analysis_failed)
        degradations.append(notice)

    payload = CaptionWindowsPlanArtifact.model_validate(
        {
            "policy_version": "caption_windows_v1",
            "source_video_artifact_id": rendered.id,
            "source_timeline_artifact_id": timeline_artifact.id,
            "fps": fps,
            "width": width,
            "height": height,
            "normal_enabled": normal_enabled,
            "emphasis_enabled": emphasis_enabled,
            "normal_safe_rect": safe_rect,
            "normal_windows": normal_windows,
            "emphasis_windows": emphasis_windows,
            "diagnostics": diagnostics,
        }
    ).model_dump(mode="json")
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_caption_windows,
                payload,
                "CaptionWindowsPlan.v1",
            )
        ],
        warnings=warnings,
        degradations=degradations,
    )


@dataclass
class _EmphasisWindowState:
    """Per-window measurement + tiered-selection bookkeeping."""

    window: dict
    base_anchors: list[dict]
    measurement: object | None = None  # OptionMeasurementResult when measured
    unavailable: str | None = None  # detector/opencv/extraction/decode failure
    short: bool = False  # window too short for 3 distinct frames
    no_candidates: bool = False  # no option envelopes survived pre-pixel filters
    safe_options: list[dict] = field(default_factory=list)
    tier: int = 0
    pushed_t3: bool = False
    rejected: tuple[int, int, int] = (0, 0, 0)  # face, scene_text, busy at applied tier


@dataclass(frozen=True)
class EmphasisAnalysisSummary:
    events_with_options: int
    tier1_events: int
    relaxed_tier2_events: int
    relaxed_tier3_events: int
    analyzable_events: int
    floor: int
    death_causes: list[dict]


def _analyze_emphasis_windows(
    *,
    video_path: str,
    temp_dir: Path,
    fps: int,
    windows: list[dict],
    diagnostics: dict,
    width: int,
    height: int,
    measure,
    font_size: float,
    outline: float,
    shadow: float,
    normal_safe_rect: dict | None,
) -> EmphasisAnalysisSummary:
    try:
        import cv2  # type: ignore
    except Exception:
        cv2 = None

    states = [
        _measure_emphasis_window(
            index=index,
            window=window,
            video_path=video_path,
            temp_dir=temp_dir,
            fps=fps,
            cv2=cv2,
            diagnostics=diagnostics,
            width=width,
            height=height,
            measure=measure,
            font_size=font_size,
            outline=outline,
            shadow=shadow,
            normal_safe_rect=normal_safe_rect,
        )
        for index, window in enumerate(windows)
    ]

    # Analyzable events can conceivably yield an option (measured, with candidates).
    analyzable = [state for state in states if state.measurement is not None]
    floor = min(EMPHASIS_MIN_EVENTS, len(analyzable))

    # Tier 1: default thresholds.
    for state in analyzable:
        options, rejected = _select_tier(state.measurement)
        if options:
            state.safe_options = options
            state.tier = 1
        state.rejected = rejected
    events_with_options = sum(1 for state in analyzable if state.safe_options)

    # Tier 2: relax scene-text/busyness for still-optionless events, only if below floor.
    relaxed_tier2 = 0
    if events_with_options < floor:
        for state in analyzable:
            if state.safe_options:
                continue
            options, rejected = _select_tier(
                state.measurement,
                scene_text_max=EMPHASIS_TIER2_SCENE_TEXT_MAX,
                busy_max=EMPHASIS_TIER2_BUSY_MAX,
            )
            if options:
                state.safe_options = options
                state.tier = 2
                state.rejected = rejected
                relaxed_tier2 += 1
        events_with_options = sum(1 for state in analyzable if state.safe_options)

    # Tier 3: single least-busy face-clear option; face stays the red line.
    relaxed_tier3 = 0
    if events_with_options < floor:
        for state in analyzable:
            if state.safe_options:
                continue
            state.pushed_t3 = True
            best = select_best_face_clear_option(state.measurement)
            if best is not None:
                state.safe_options = [best]
                state.tier = 3
                state.rejected = (count_face_blocked(state.measurement), 0, 0)
                relaxed_tier3 += 1
        events_with_options = sum(1 for state in analyzable if state.safe_options)

    death_causes: list[dict] = []
    for state in states:
        _finalize_emphasis_window(state, diagnostics, death_causes)

    events_with_options = sum(1 for state in states if state.window.get("caption_options"))
    tier1_events = sum(1 for state in states if state.tier == 1 and state.safe_options)
    diagnostics["events_with_options"] = events_with_options
    diagnostics["relaxed_tier2_events"] = relaxed_tier2
    diagnostics["relaxed_tier3_events"] = relaxed_tier3
    return EmphasisAnalysisSummary(
        events_with_options=events_with_options,
        tier1_events=tier1_events,
        relaxed_tier2_events=relaxed_tier2,
        relaxed_tier3_events=relaxed_tier3,
        analyzable_events=len(analyzable),
        floor=floor,
        death_causes=death_causes,
    )


def _select_tier(measurement, **kwargs) -> tuple[list[dict], tuple[int, int, int]]:
    options, rejected_face, rejected_text, rejected_busy = select_options_at_thresholds(
        measurement, **kwargs
    )
    return options, (rejected_face, rejected_text, rejected_busy)


def _measure_emphasis_window(
    *,
    index: int,
    window: dict,
    video_path: str,
    temp_dir: Path,
    fps: int,
    cv2,
    diagnostics: dict,
    width: int,
    height: int,
    measure,
    font_size: float,
    outline: float,
    shadow: float,
    normal_safe_rect: dict | None,
) -> _EmphasisWindowState:
    base_anchors = list(window.get("anchor_candidates") or [])
    diagnostics["generated_anchor_candidates"] += len(base_anchors)
    option_candidates = build_caption_option_candidates(
        event_id=str(window.get("event_id") or ""),
        text=str(window.get("text") or ""),
        anchors=base_anchors,
        width=width,
        height=height,
        measure=measure,
        font_size=font_size,
        outline=outline,
        shadow=shadow,
        normal_safe_rect=normal_safe_rect,
        hero_eligible=bool(window.get("hero_eligible")),
    )
    window.pop("hero_eligible", None)
    state = _EmphasisWindowState(window=window, base_anchors=base_anchors)
    if not option_candidates:
        state.no_candidates = True
        return state
    sample_frames = sample_frame_indices(
        int(window.get("start_frame") or 0),
        int(window.get("end_frame") or 0),
    )
    if len(sample_frames) != 3:
        # A too-short window is not a detector failure; it must not poison the
        # visual-analysis-failed signal (which is reserved for real detector faults).
        state.short = True
        return state
    if cv2 is None:
        state.unavailable = "opencv"
        return state
    sample_times = [frame / fps for frame in sample_frames]
    try:
        extracted = extract_frames_for_times(
            video_path,
            sample_times,
            temp_dir=str(temp_dir / f"event_{index:03d}"),
            max_long_side=1024,
        )
    except Exception:
        extracted = []
    if len(extracted) != 3:
        state.unavailable = "frame_extraction"
        return state
    images = [cv2.imread(path) for _time, path in extracted]
    if any(image is None for image in images):
        state.unavailable = "frame_decode"
        return state
    try:
        result = measure_option_candidates(
            images=images,
            sample_frames=sample_frames,
            option_candidates=option_candidates,
        )
    except Exception:
        state.unavailable = "visual_analysis"
        return state
    if result.unavailable_detector is not None:
        state.unavailable = result.unavailable_detector
        return state
    state.measurement = result
    return state


def _record_unavailable(diagnostics: dict, detector: str | None) -> None:
    if detector is not None and detector not in diagnostics["unavailable_detectors"]:
        diagnostics["unavailable_detectors"].append(detector)


def _finalize_emphasis_window(
    state: _EmphasisWindowState, diagnostics: dict, death_causes: list[dict]
) -> None:
    window = state.window
    base_anchors = state.base_anchors
    event_id = str(window.get("event_id") or "")

    if state.no_candidates:
        diagnostics["rejected_anchor_candidates"] += len(base_anchors)
        diagnostics["events_without_options"] += 1
        window["anchor_candidates"] = []
        window["caption_options"] = []
        death_causes.append({"event_id": event_id, "cause": "no_option_candidates"})
        return
    if state.short:
        diagnostics["rejected_anchor_candidates"] += len(base_anchors)
        diagnostics["events_without_options"] += 1
        _record_unavailable(diagnostics, "window_too_short")
        window["anchor_candidates"] = []
        window["caption_options"] = []
        death_causes.append({"event_id": event_id, "cause": "window_too_short"})
        return
    if state.measurement is None:
        diagnostics["visual_analysis_failed"] = True
        diagnostics["events_without_options"] += 1
        _record_unavailable(diagnostics, state.unavailable)
        diagnostics["rejected_anchor_candidates"] += len(base_anchors)
        window["anchor_candidates"] = []
        window["caption_options"] = []
        death_causes.append({"event_id": event_id, "cause": state.unavailable})
        return

    diagnostics["sampled_frames"] += 3
    diagnostics["rejected_face"] += state.rejected[0]
    diagnostics["rejected_scene_text"] += state.rejected[1]
    diagnostics["rejected_busy"] += state.rejected[2]
    persisted_anchors, options, cap_diagnostics = finalize_safe_caption_options(
        anchors=base_anchors,
        safe_options=state.safe_options,
    )
    diagnostics["safe_anchor_candidates"] += cap_diagnostics["safe_anchor_candidates"]
    diagnostics["anchors_pruned_by_cap"] += cap_diagnostics["anchors_pruned_by_cap"]
    diagnostics["options_pruned_by_cap"] += cap_diagnostics["options_pruned_by_cap"]
    diagnostics["rejected_anchor_candidates"] += max(
        0, len(base_anchors) - cap_diagnostics["safe_anchor_candidates"]
    )
    window["anchor_candidates"] = persisted_anchors
    window["caption_options"] = options
    if not options:
        diagnostics["events_without_options"] += 1
        death_causes.append(
            {
                "event_id": event_id,
                "cause": "face_overlap" if state.pushed_t3 else "safety_rejected",
            }
        )


def _append_degradation(
    ctx: NodeContext,
    warnings: list[WarningCode],
    degradations: list[DegradationNotice],
    code: WarningCode,
    message: str,
) -> None:
    warnings.append(code)
    degradations.append(
        degradation_notice(
            code,
            message,
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        )
    )
