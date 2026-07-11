"""CaptionWindowPlanning: deterministic caption timing and pixel-safe options."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import ArtifactKind, DegradationNotice, NodeStatus, WarningCode
from packages.core.contracts.artifacts import CaptionWindowsPlanArtifact
from packages.core.workflow import NodeOutput
from packages.media.annotation.sensors.frames import extract_frames_for_times
from packages.production.pipeline._caption_visual_safety import (
    evaluate_option_safety,
    sample_frame_indices,
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

        if emphasis_enabled:
            final_ass_emphasis_font_size = final_ass_font_size
            emphasis_measure, _emphasis_metrics_source = make_text_measurer(
                normal_metrics,
                float(final_ass_emphasis_font_size),
            )
            colors = _subtitle_colors(state.request.subtitle.style_preset)
            _analyze_emphasis_windows(
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
) -> None:
    try:
        import cv2  # type: ignore
    except Exception:
        cv2 = None

    for index, window in enumerate(windows):
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
        if not option_candidates:
            diagnostics["rejected_anchor_candidates"] += len(base_anchors)
            diagnostics["events_without_options"] += 1
            window["anchor_candidates"] = []
            window["caption_options"] = []
            continue
        sample_frames = sample_frame_indices(
            int(window.get("start_frame") or 0),
            int(window.get("end_frame") or 0),
        )
        unavailable = None
        images = []
        if len(sample_frames) == 3 and cv2 is not None:
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
            if len(extracted) == 3:
                images = [cv2.imread(path) for _time, path in extracted]
                if any(image is None for image in images):
                    unavailable = "frame_decode"
            else:
                unavailable = "frame_extraction"
        else:
            unavailable = "opencv" if cv2 is None else "frame_extraction"

        if unavailable is None:
            try:
                result = evaluate_option_safety(
                    images=images,
                    sample_frames=sample_frames,
                    option_candidates=option_candidates,
                )
                unavailable = result.unavailable_detector
            except Exception:
                result = None
                unavailable = "visual_analysis"
        else:
            result = None

        if unavailable is not None or result is None:
            diagnostics["visual_analysis_failed"] = True
            diagnostics["events_without_options"] += 1
            if unavailable not in diagnostics["unavailable_detectors"]:
                diagnostics["unavailable_detectors"].append(unavailable)
            diagnostics["rejected_anchor_candidates"] += len(base_anchors)
            window["anchor_candidates"] = []
            window["caption_options"] = []
            continue

        diagnostics["sampled_frames"] += 3
        diagnostics["rejected_face"] += result.rejected_face
        diagnostics["rejected_scene_text"] += result.rejected_scene_text
        diagnostics["rejected_busy"] += result.rejected_busy
        persisted_anchors, options, cap_diagnostics = finalize_safe_caption_options(
            anchors=base_anchors,
            safe_options=result.options,
        )
        diagnostics["safe_anchor_candidates"] += cap_diagnostics[
            "safe_anchor_candidates"
        ]
        diagnostics["anchors_pruned_by_cap"] += cap_diagnostics[
            "anchors_pruned_by_cap"
        ]
        diagnostics["options_pruned_by_cap"] += cap_diagnostics[
            "options_pruned_by_cap"
        ]
        diagnostics["rejected_anchor_candidates"] += max(
            0,
            len(base_anchors) - cap_diagnostics["safe_anchor_candidates"],
        )
        window["anchor_candidates"] = persisted_anchors
        window["caption_options"] = options
        if not options:
            diagnostics["events_without_options"] += 1


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
