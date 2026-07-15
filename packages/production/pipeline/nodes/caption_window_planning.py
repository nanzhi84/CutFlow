"""CaptionWindowPlanning: deterministic caption timing and pixel-safe options."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from packages.core.contracts import (
    ArtifactKind,
    DegradationNotice,
    ErrorCode,
    NodeStatus,
    SpeechTokenTiming,
    WarningCode,
)
from packages.core.contracts.artifacts import CaptionWindowsPlanArtifact
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.annotation.sensors.frames import (
    extract_frames_for_indices,
    extract_frames_for_times,
)
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
    build_normal_caption_position_candidates,
    compile_normal_windows,
    emphasis_conflict_graph,
    finalize_safe_caption_options,
    max_feasible_emphasis_count,
    normal_safe_rect,
    timeline_cut_frames,
)
from packages.production.pipeline._caption_effects import effect_envelope, normal_reference_geometry
from packages.production.pipeline._font_metrics import (
    font_text_safety_issue,
    load_font_metrics,
    make_text_measurer,
)
from packages.production.pipeline._fonts import (
    DEFAULT_FONT_SENTINEL,
    caption_font_asset_ids,
    distinct_font_assets_share_family,
    is_font_collection,
    resolve_font_asset,
)
from packages.production.pipeline._huazi_candidates import normal_caption_top_y
from packages.production.pipeline._materialize import (
    _subtitle_colors,
    _subtitle_font_size,
    _subtitle_position,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline._speech_timing import assign_token_ownership
from packages.production.pipeline._subtitles import (
    _ASS_MARGIN_L,
    _ASS_MARGIN_R,
    ass_font_size,
)
from packages.production.pipeline.nodes._creative_intent import load_creative_intent

_EAW_RENDER_SAFETY_SCALE = 4.0 / 3.0


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
    timing_tokens: list[SpeechTokenTiming] = []
    for item in alignment.get("tokens") or []:
        if not isinstance(item, dict):
            continue
        try:
            timing_tokens.append(SpeechTokenTiming.model_validate(item))
        except Exception:
            continue
    tokens = [
        item.model_dump(mode="json")
        for item in assign_token_ownership(
            timing_tokens,
            script=state.request.script,
            units=list(units),
        )
    ]
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
        "token_claim_failures": 0,
        "caption_gap_clamps": 0,
        "emphasis_conflicts": 0,
        "max_feasible_emphasis_count": 0,
        "normal_generated_candidates": 0,
        "normal_dynamic_positioned": 0,
        "normal_relaxed_safety": 0,
        "normal_rejected_face": 0,
        "normal_rejected_scene_text": 0,
        "normal_rejected_busy": 0,
    }

    with tempfile.TemporaryDirectory(prefix="cutagent-caption-window-") as directory:
        temp_dir = Path(directory)
        requested_normal_font_id = state.request.subtitle.font_id
        requested_emphasis_font_id = state.request.subtitle.emphasis_font_id
        normal_font_id, emphasis_font_id = caption_font_asset_ids(
            requested_normal_font_id,
            requested_emphasis_font_id,
        )
        normal_font_defaulted = not requested_normal_font_id or (
            requested_normal_font_id == DEFAULT_FONT_SENTINEL
        )
        emphasis_font_defaulted = (
            not requested_emphasis_font_id
            or requested_emphasis_font_id == DEFAULT_FONT_SENTINEL
        ) and normal_font_defaulted
        resolved_font = None
        resolved_emphasis_font = None
        normal_metrics = None
        emphasis_metrics = None
        normal_unresolved_font_id = None
        emphasis_unresolved_font_id = None
        # The emphasis layer falls back to the normal family when its explicit
        # font cannot be resolved, even when ordinary captions are disabled.
        if (normal_enabled or emphasis_enabled) and normal_font_id:
            resolved_font, normal_unresolved_font_id = resolve_font_asset(
                font_asset_id=normal_font_id,
                runtime_dir=temp_dir / "fonts",
                source_artifact_for_asset=ctx.source_artifact_for_asset,
                artifact_path=ctx.artifact_path,
            )
            if normal_unresolved_font_id:
                detail = (
                    "默认普通字幕字体资产缺失；请先执行 "
                    "python scripts/import_font_assets.py 再生成视频。"
                    if normal_font_defaulted
                    else f"指定字幕字体（{normal_unresolved_font_id}）文件无法加载；"
                    "无法证明字幕像素安全。"
                )
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    detail,
                    retryable=False,
                )
            if is_font_collection(resolved_font):
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    f"字幕字体（{normal_font_id}）是多字形 TTC 集合；libass 无法保证使用"
                    "规划时量测的 face，请改用单字体 TTF/OTF/WOFF 文件。",
                    retryable=False,
                )
            normal_metrics = load_font_metrics(resolved_font.source_path) if resolved_font else None
            if resolved_font is not None and normal_metrics is None:
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    f"无法读取字幕字体（{normal_font_id}）的字形度量；无法证明字幕像素安全。",
                    retryable=False,
                )
            if normal_enabled and resolved_font is not None:
                issue = font_text_safety_issue(
                    resolved_font.source_path,
                    [str(unit.get("text") or "") for unit in units],
                )
                if issue:
                    raise NodeExecutionError(
                        ErrorCode.render_subtitle_failed,
                        f"字幕字体（{normal_font_id}）无法安全覆盖当前文本（{issue}）；"
                        "已停止规划以避免字形越出安全区。",
                        retryable=False,
                    )
        if emphasis_enabled and emphasis_font_id:
            if emphasis_font_id == normal_font_id:
                resolved_emphasis_font = resolved_font
                emphasis_metrics = normal_metrics
            else:
                resolved_emphasis_font, emphasis_unresolved_font_id = resolve_font_asset(
                    font_asset_id=emphasis_font_id,
                    runtime_dir=temp_dir / "fonts",
                    source_artifact_for_asset=ctx.source_artifact_for_asset,
                    artifact_path=ctx.artifact_path,
                )
                if emphasis_unresolved_font_id:
                    detail = (
                        "默认花字字体资产缺失；请先执行 "
                        "python scripts/import_font_assets.py 再生成视频。"
                        if emphasis_font_defaulted
                        else f"指定花字字体（{emphasis_unresolved_font_id}）文件无法加载；"
                        "无法证明花字像素安全。"
                    )
                    raise NodeExecutionError(
                        ErrorCode.render_subtitle_failed,
                        detail,
                        retryable=False,
                    )
                else:
                    if is_font_collection(resolved_emphasis_font):
                        raise NodeExecutionError(
                            ErrorCode.render_subtitle_failed,
                            f"花字字体（{emphasis_font_id}）是多字形 TTC 集合；libass 无法保证"
                            "使用规划时量测的 face，请改用单字体 TTF/OTF/WOFF 文件。",
                            retryable=False,
                        )
                    emphasis_metrics = (
                        load_font_metrics(resolved_emphasis_font.source_path)
                        if resolved_emphasis_font
                        else None
                    )
                if resolved_emphasis_font is not None and emphasis_metrics is None:
                    raise NodeExecutionError(
                        ErrorCode.render_subtitle_failed,
                        f"无法读取花字字体（{emphasis_font_id}）的字形度量；"
                        "无法证明花字像素安全。",
                        retryable=False,
                    )
            if distinct_font_assets_share_family(
                normal_font_id,
                resolved_font,
                emphasis_font_id,
                resolved_emphasis_font,
            ):
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    "普通字幕与花字选择了同一字体家族的不同文件；libass 无法保证使用"
                    "规划时量测的字形，请改用不同家族或同一个字体资产。",
                    retryable=False,
                )
        requested_font_size = _subtitle_font_size(
            state.request.subtitle.style_preset,
            state.request.subtitle.font_size,
        )
        final_ass_font_size = ass_font_size(requested_font_size, height=height)
        measure, metrics_source = _caption_planning_measurer(
            normal_metrics, float(final_ass_font_size)
        )
        normal_outline, normal_shadow_x, normal_shadow_y = normal_reference_geometry(height=height)
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
            max_lines=3,
            max_line_width_px=width * 0.62,
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
        if normal_enabled:
            normal_frame_images = _extract_window_frame_images(
                video_path=str(ctx.artifact_path(rendered)),
                temp_dir=temp_dir / "normal_frames",
                windows=normal_windows,
            )
            _analyze_normal_windows(
                video_path=str(ctx.artifact_path(rendered)),
                temp_dir=temp_dir / "normal_frames",
                fps=fps,
                windows=normal_windows,
                diagnostics=diagnostics,
                width=width,
                height=height,
                measure=measure,
                font_size=float(final_ass_font_size),
                outline=normal_outline,
                shadow_x=normal_shadow_x,
                shadow_y=normal_shadow_y,
                requested_position_y=position_y,
                frame_images=normal_frame_images,
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
            if resolved_emphasis_font is not None:
                issue = font_text_safety_issue(
                    resolved_emphasis_font.source_path,
                    [str(window.get("text") or "") for window in emphasis_windows],
                )
                if issue:
                    raise NodeExecutionError(
                        ErrorCode.render_subtitle_failed,
                        f"花字字体（{emphasis_font_id}）无法安全覆盖当前文本（{issue}）；"
                        "已停止规划以避免字形越出安全区。",
                        retryable=False,
                    )
            emphasis_frame_images = _extract_window_frame_images(
                video_path=str(ctx.artifact_path(rendered)),
                temp_dir=temp_dir / "frames",
                windows=emphasis_windows,
            )
            final_ass_emphasis_font_size = final_ass_font_size
            emphasis_measure, _emphasis_metrics_source = _caption_planning_measurer(
                emphasis_metrics,
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
                normal_windows=normal_windows,
                frame_images=emphasis_frame_images,
            )

    conflicts = emphasis_conflict_graph(emphasis_windows, fps=fps)
    max_feasible_count = max_feasible_emphasis_count(emphasis_windows, fps=fps)
    diagnostics["emphasis_conflicts"] = len(conflicts)
    diagnostics["max_feasible_emphasis_count"] = max_feasible_count

    if diagnostics["normal_relaxed_safety"]:
        notice = degradation_notice(
            WarningCode.caption_normal_relaxed_safety,
            "部分普通字幕没有同时通过场内文字/繁忙区阈值，已选择人脸安全且风险最低的位置。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        ).model_copy(
            update={
                "details": {
                    "relaxed_windows": diagnostics["normal_relaxed_safety"],
                    "dynamic_windows": diagnostics["normal_dynamic_positioned"],
                }
            }
        )
        warnings.append(WarningCode.caption_normal_relaxed_safety)
        degradations.append(notice)

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
        and max_feasible_count < EMPHASIS_MIN_EVENTS
    ):
        notice = degradation_notice(
            WarningCode.caption_emphasis_below_floor,
            f"花字最大合法数量未达下限（{max_feasible_count}/"
            f"{EMPHASIS_MIN_EVENTS}）；已尽可能放宽后仍无法安全放置更多花字。",
            node_id=ctx.node_run.node_id,
            affects_true_yield=False,
        ).model_copy(
            update={
                "details": {
                    "events_with_options": emphasis_summary.events_with_options,
                    "max_feasible_count": max_feasible_count,
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
            "policy_version": "caption_windows_v2",
            "source_video_artifact_id": rendered.id,
            "source_timeline_artifact_id": timeline_artifact.id,
            "fps": fps,
            "width": width,
            "height": height,
            "normal_enabled": normal_enabled,
            "emphasis_enabled": emphasis_enabled,
            "normal_font_asset_id": normal_font_id
            if normal_enabled or emphasis_enabled
            else None,
            "emphasis_font_asset_id": emphasis_font_id if emphasis_enabled else None,
            "normal_safe_rect": safe_rect,
            "normal_windows": normal_windows,
            "emphasis_windows": emphasis_windows,
            "emphasis_conflicts": conflicts,
            "max_feasible_emphasis_count": max_feasible_count,
            "diagnostics": diagnostics,
        }
    ).model_dump(mode="json")
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_caption_windows,
                payload,
                "CaptionWindowsPlan.v2",
            )
        ],
        warnings=warnings,
        degradations=degradations,
    )


def _caption_planning_measurer(metrics, font_size: float):
    """Use real hmtx widths or a fail-safe full-cell EAW envelope.

    The generic EAW measurer intentionally models UI/CSS width at roughly 0.75
    of the ASS cell height. CaptionWindowPlanning instead owns collision safety
    for the final libass pixels; when no font file is available, invert that
    calibration so CJK glyphs reserve a conservative full cell. Legacy caption
    compilation keeps its historical width behavior.
    """

    measure, source = make_text_measurer(metrics, font_size)
    if source != "eaw_fallback":
        return measure, source

    def conservative_measure(text: str) -> float:
        return measure(text) * _EAW_RENDER_SAFETY_SCALE

    return conservative_measure, source


def _extract_window_frame_images(
    *,
    video_path: str,
    temp_dir: Path,
    windows: list[dict],
) -> dict[int, object | None]:
    """Decode all unique three-frame observations for one caption layer once."""

    frame_indices = sorted(
        {
            frame
            for window in windows
            for frame in sample_frame_indices(
                int(window.get("start_frame") or 0),
                int(window.get("end_frame") or 0),
            )
        }
    )
    if not frame_indices:
        return {}
    try:
        import cv2  # type: ignore
    except Exception:
        return {}
    try:
        extracted = extract_frames_for_indices(
            video_path,
            frame_indices,
            temp_dir=str(temp_dir),
            max_long_side=1024,
        )
    except Exception:
        return {}
    if len(extracted) != len(frame_indices):
        return {}
    return {frame: cv2.imread(path) for frame, path in extracted}


def _analyze_normal_windows(
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
    shadow_x: float,
    shadow_y: float,
    requested_position_y: float,
    frame_images: dict[int, object | None] | None = None,
) -> None:
    """Materialize one deterministic, final-frame-safe rect per normal cue."""

    try:
        import cv2  # type: ignore
    except Exception:
        cv2 = None
    previous_anchor_id: str | None = None
    for index, window in enumerate(windows):
        candidates = build_normal_caption_position_candidates(
            window_id=str(window.get("window_id") or f"caption_{index + 1:03d}"),
            lines=list(window.get("lines") or []),
            width=width,
            height=height,
            measure=measure,
            font_size=font_size,
            outline=outline,
            shadow_x=shadow_x,
            shadow_y=shadow_y,
            requested_position_y=requested_position_y,
            vertical_shift_px=effect_envelope(str(window.get("effect_id") or "none"))[1],
        )
        diagnostics["normal_generated_candidates"] += len(candidates)
        sample_frames = sample_frame_indices(
            int(window.get("start_frame") or 0),
            int(window.get("end_frame") or 0),
        )
        unavailable: str | None = None
        measurement = None
        if not candidates:
            unavailable = "normal_no_position_candidates"
        elif len(sample_frames) != 3:
            unavailable = "normal_window_too_short"
        elif cv2 is None:
            unavailable = "opencv"
        elif frame_images is not None:
            images = [frame_images.get(frame) for frame in sample_frames]
            if any(image is None for image in images):
                unavailable = "frame_decode" if frame_images else "frame_extraction"
            else:
                try:
                    measurement = measure_option_candidates(
                        images=images,
                        sample_frames=sample_frames,
                        option_candidates=candidates,
                    )
                except Exception:
                    unavailable = "visual_analysis"
                if measurement is not None and measurement.unavailable_detector is not None:
                    unavailable = measurement.unavailable_detector
                    measurement = None
        else:
            try:
                extracted = extract_frames_for_times(
                    video_path,
                    [frame / fps for frame in sample_frames],
                    temp_dir=str(temp_dir / f"window_{index:03d}"),
                    max_long_side=1024,
                )
            except Exception:
                extracted = []
            if len(extracted) != 3:
                unavailable = "frame_extraction"
            else:
                images = [cv2.imread(path) for _time, path in extracted]
                if any(image is None for image in images):
                    unavailable = "frame_decode"
                else:
                    try:
                        measurement = measure_option_candidates(
                            images=images,
                            sample_frames=sample_frames,
                            option_candidates=candidates,
                        )
                    except Exception:
                        unavailable = "visual_analysis"
                    if measurement is not None and measurement.unavailable_detector is not None:
                        unavailable = measurement.unavailable_detector
                        measurement = None

        choice = None
        if measurement is not None:
            diagnostics["sampled_frames"] += 3
            safe, rejected_face, rejected_text, rejected_busy = select_options_at_thresholds(
                measurement
            )
            diagnostics["normal_rejected_face"] += rejected_face
            diagnostics["normal_rejected_scene_text"] += rejected_text
            diagnostics["normal_rejected_busy"] += rejected_busy
            if safe:
                choice = min(
                    safe,
                    key=lambda item: (
                        float(item.get("busy_score") or 0.0)
                        + float(item.get("scene_text_overlap") or 0.0) * 0.5
                        + (
                            0.0
                            if previous_anchor_id
                            and str(item.get("anchor_id") or "") == previous_anchor_id
                            else 0.04
                        ),
                        int(item.get("position_rank") or 0),
                        str(item.get("caption_option_id") or ""),
                    ),
                )
            else:
                choice = select_best_face_clear_option(measurement)
                if choice is not None:
                    diagnostics["normal_relaxed_safety"] += 1

        if choice is None and measurement is not None:
            raise NodeExecutionError(
                ErrorCode.render_subtitle_failed,
                "普通字幕没有任何通过人脸安全红线的位置："
                f"{window.get('window_id') or f'caption_{index + 1:03d}'}",
                retryable=False,
            )
        if choice is None:
            diagnostics["visual_analysis_failed"] = True
            _record_unavailable(diagnostics, unavailable or "normal_no_face_clear_position")
            raise NodeExecutionError(
                ErrorCode.render_subtitle_failed,
                "普通字幕视觉安全分析无法完成，已拒绝未经人脸红线验证的烧录："
                f"{window.get('window_id') or f'caption_{index + 1:03d}'}"
                f"（{unavailable or 'normal_no_face_clear_position'}）",
                retryable=False,
            )
        diagnostics["normal_dynamic_positioned"] += 1
        rect = choice.get("rect") or choice.get("safety_envelope")
        if isinstance(rect, dict):
            window["rect"] = dict(rect)
        safety_envelope = choice.get("safety_envelope") or rect
        if isinstance(safety_envelope, dict):
            window["safety_envelope"] = dict(safety_envelope)
        window["text_align"] = str(choice.get("text_align") or "center")
        previous_anchor_id = str(choice.get("anchor_id") or "") or previous_anchor_id


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
    normal_windows: list[dict] | None = None,
    frame_images: dict[int, object | None] | None = None,
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
            normal_windows=normal_windows,
            frame_images=frame_images,
        )
        for index, window in enumerate(windows)
    ]

    # Analyzable events can conceivably yield an option (measured, with candidates).
    # The floor is a feasible-subset target, not a raw event counter: mutually
    # conflicting candidates must never trigger meaningless safety relaxation.
    analyzable = [state for state in states if state.measurement is not None]
    maximum_possible = _max_feasible_state_count(
        analyzable,
        fps=fps,
        assume_selectable=True,
    )
    floor = min(EMPHASIS_MIN_EVENTS, maximum_possible)

    # Tier 1: default thresholds.
    for state in analyzable:
        options, rejected = _select_tier(state.measurement)
        if options:
            state.safe_options = options
            state.tier = 1
        state.rejected = rejected
    events_with_options = sum(1 for state in analyzable if state.safe_options)
    feasible_with_options = _max_feasible_state_count(analyzable, fps=fps)

    # Tier 2: relax scene-text/busyness for still-optionless events, only if below floor.
    relaxed_tier2 = 0
    if feasible_with_options < floor:
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
        feasible_with_options = _max_feasible_state_count(analyzable, fps=fps)

    # Tier 3: single least-busy face-clear option; face stays the red line.
    relaxed_tier3 = 0
    if feasible_with_options < floor:
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


def _max_feasible_state_count(
    states: list[_EmphasisWindowState],
    *,
    fps: int,
    assume_selectable: bool = False,
) -> int:
    windows = []
    for state in states:
        options = [{"caption_option_id": "candidate"}] if assume_selectable else state.safe_options
        if not options:
            continue
        windows.append({**state.window, "caption_options": options})
    return max_feasible_emphasis_count(windows, fps=fps)


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
    normal_windows: list[dict],
    frame_images: dict[int, object | None] | None,
) -> _EmphasisWindowState:
    base_anchors = list(window.get("anchor_candidates") or [])
    diagnostics["generated_anchor_candidates"] += len(base_anchors)
    event_start = int(window.get("start_frame") or 0)
    event_end = int(window.get("end_frame") or 0)
    overlapping_normal_rects = []
    for normal_window in normal_windows or []:
        if (
            int(normal_window.get("start_frame") or 0) >= event_end
            or int(normal_window.get("end_frame") or 0) <= event_start
        ):
            continue
        envelope = normal_window.get("safety_envelope") or normal_window.get("rect")
        if isinstance(envelope, dict):
            overlapping_normal_rects.append(dict(envelope))
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
        normal_safe_rect=normal_safe_rect if not overlapping_normal_rects else None,
        normal_safe_rects=overlapping_normal_rects,
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
    if frame_images is not None:
        images = [frame_images.get(frame) for frame in sample_frames]
        if not frame_images:
            state.unavailable = "frame_extraction"
            return state
    else:
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
