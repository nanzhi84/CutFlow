"""SubtitleAndBgmMix node: burn subtitles, mix voice + BGM into the final video."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import (
    ArtifactKind,
    DegradationNotice,
    ErrorCode,
    NodeStatus,
    WarningCode,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.rendering import validate_rendered_output
from packages.media.video.ffmpeg import FfmpegCommandError, probe_audio_channels, probe_media
from packages.production.pipeline._caption_display import (
    CaptionDisplayResult,
    compile_caption_display,
    compile_planned_caption_display,
)
from packages.production.pipeline._ffmpeg import (
    SfxMixEvent,
    ffmpeg_filter_available,
    render_final_media,
)
from packages.production.pipeline._font_metrics import load_font_metrics, make_text_measurer
from packages.production.pipeline._fonts import ResolvedFont, resolve_font_asset
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline._sfx_events import plan_caption_sfx_events
from packages.production.pipeline._subtitles import (
    _ASS_MARGIN_L,
    _ASS_MARGIN_R,
    _subtitle_layer_enabled,
    ass_font_size,
    write_ass_subtitles,
)

def _font_asset_id(style: dict, *, emphasis: bool = False) -> str | None:
    subtitle = style.get("subtitle") if isinstance(style.get("subtitle"), dict) else {}
    font = style.get("font") if isinstance(style.get("font"), dict) else {}
    if emphasis:
        return (
            style.get("emphasis_font_asset_id")
            or (font or {}).get("emphasis_font_id")
            or (subtitle or {}).get("emphasis_font_id")
            or _font_asset_id(style)
        )
    return (
        style.get("font_asset_id")
        or (font or {}).get("font_id")
        or (subtitle or {}).get("font_id")
    )


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    rendered = state.require(ArtifactKind.video_rendered)
    audio = state.require(ArtifactKind.audio_tts)
    timeline = state.require(ArtifactKind.plan_timeline).payload or {}
    style = state.require(ArtifactKind.plan_style).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    caption_windows_artifact = state.artifacts.get(ArtifactKind.plan_caption_windows)
    caption_windows = (
        caption_windows_artifact.payload or {} if caption_windows_artifact is not None else None
    )
    fps = int(timeline.get("fps") or state.request.output.fps)
    total_frames = int(timeline.get("total_frames") or 0)
    duration = total_frames / fps if total_frames else float(rendered.media_info.duration_sec or 0)
    subtitle_artifact = None
    caption_display_payload: dict | None = None
    degradations: list[DegradationNotice] = []
    warnings: list[WarningCode] = []
    try:
        with tempfile.TemporaryDirectory(prefix="cutagent-final-") as directory:
            temp_dir = Path(directory)
            subtitle_layers_enabled = bool(
                state.request.subtitle.enabled
                and (state.request.subtitle.normal_enabled or state.request.subtitle.emphasis_enabled)
            )
            subtitle_path = temp_dir / "subtitle.ass" if subtitle_layers_enabled else None
            fonts_dir = temp_dir / "fonts"
            resolved_font: ResolvedFont | None = None
            resolved_emphasis_font: ResolvedFont | None = None
            if subtitle_path is not None:
                normal_font_asset_id = _font_asset_id(style)
                emphasis_font_asset_id = _font_asset_id(style, emphasis=True)
                if caption_windows is not None:
                    # Caption v3 uses one resolved font across normal/emphasis/hero.
                    emphasis_font_asset_id = normal_font_asset_id
                resolved_font, unresolved_font_id = resolve_font_asset(
                    font_asset_id=normal_font_asset_id,
                    runtime_dir=fonts_dir,
                    source_artifact_for_asset=ctx.source_artifact_for_asset,
                    artifact_path=ctx.artifact_path,
                    media_assets=ctx.repository.media_assets,
                )
                unresolved_emphasis_font_id: str | None = None
                if emphasis_font_asset_id and emphasis_font_asset_id != normal_font_asset_id:
                    resolved_emphasis_font, unresolved_emphasis_font_id = resolve_font_asset(
                        font_asset_id=emphasis_font_asset_id,
                        runtime_dir=fonts_dir,
                        source_artifact_for_asset=ctx.source_artifact_for_asset,
                        artifact_path=ctx.artifact_path,
                        media_assets=ctx.repository.media_assets,
                    )
                else:
                    resolved_emphasis_font = resolved_font
                # No silent fallback: a selected font whose file can't be staged
                # must surface, not quietly burn the default Arial.
                if unresolved_font_id:
                    degradations.append(
                        degradation_notice(
                            WarningCode.font_resolution_failed,
                            f"指定字幕字体（{unresolved_font_id}）文件无法加载，已使用默认字体。",
                            node_id=ctx.node_run.node_id,
                        )
                    )
                    warnings.append(WarningCode.font_resolution_failed)
                if unresolved_emphasis_font_id:
                    degradations.append(
                        degradation_notice(
                            WarningCode.font_resolution_failed,
                            f"指定花字字体（{unresolved_emphasis_font_id}）文件无法加载，已使用普通字幕字体。",
                            node_id=ctx.node_run.node_id,
                        )
                    )
                    warnings.append(WarningCode.font_resolution_failed)
                width = state.request.output.width
                height = state.request.output.height
                subtitle_style = (
                    style.get("subtitle") if isinstance(style.get("subtitle"), dict) else {}
                ) or {}
                normal_enabled = _subtitle_layer_enabled(subtitle_style, "normal_enabled")
                emphasis_enabled = _subtitle_layer_enabled(subtitle_style, "emphasis_enabled")
                caption_font_size = ass_font_size(subtitle_style.get("font_size"), height=height)
                if caption_windows is not None:
                    # The v2 post-process chain consumes frame-authoritative windows.
                    # No line breaking or font measurement belongs in the renderer.
                    display = compile_planned_caption_display(
                        caption_windows=caption_windows,
                        normal_enabled=normal_enabled,
                        emphasis_enabled=emphasis_enabled,
                        overlay_events=list(style.get("overlay_events") or []),
                    )
                else:
                    # Historical runs have no plan.caption_windows artifact. Keep the
                    # old compiler only at this explicit read boundary.
                    metrics = load_font_metrics(resolved_font.source_path) if resolved_font else None
                    if resolved_font is not None and metrics is None:
                        degradations.append(
                            degradation_notice(
                                WarningCode.font_metrics_fallback,
                                "无法读取所选字体度量，已按估算宽度断行（可能与实际渲染略有偏差）。",
                                node_id=ctx.node_run.node_id,
                            )
                        )
                        warnings.append(WarningCode.font_metrics_fallback)
                    measure, metrics_source = make_text_measurer(
                        metrics, float(caption_font_size)
                    )
                    display = compile_caption_display(
                        units=list(narration.get("units") or []),
                        resolution=(width, height),
                        margin_l=_ASS_MARGIN_L,
                        margin_r=_ASS_MARGIN_R,
                        measure=measure,
                        metrics_source=metrics_source,
                        normal_enabled=normal_enabled,
                        emphasis_enabled=emphasis_enabled,
                        overlay_events=list(style.get("overlay_events") or []),
                    )
                if caption_windows is not None:
                    planned_by_range = {
                        (
                            int(item.get("start_frame") or 0),
                            int(item.get("end_frame") or 0),
                        ): item
                        for item in (caption_windows.get("normal_windows") or [])
                        if isinstance(item, dict)
                    }
                    caption_cues = []
                    for cue in display.normal_cues:
                        key = (round(cue.start * fps), round(cue.end * fps))
                        planned = planned_by_range.get(key, {})
                        caption_cues.append(
                            {
                                "start": cue.start,
                                "end": cue.end,
                                "lines": cue.lines,
                                "line_starts": [
                                    int(frame) / fps
                                    for frame in (planned.get("line_start_frames") or [])
                                ],
                                "effect_id": planned.get("effect_id") or "none",
                            }
                        )
                else:
                    caption_cues = [
                        {"start": cue.start, "end": cue.end, "lines": cue.lines}
                        for cue in display.normal_cues
                    ]
                animation_fallbacks = write_ass_subtitles(
                    subtitle_path,
                    style=style,
                    width=width,
                    height=height,
                    caption_cues=caption_cues,
                    overlay_events=display.emphasis_events,
                    font_name=resolved_font.family_name if resolved_font else None,
                    emphasis_font_name=(
                        resolved_emphasis_font.family_name if resolved_emphasis_font else None
                    ),
                )
                if animation_fallbacks:
                    display.diagnostics.animation_fallbacks = len(animation_fallbacks)
                    degradations.append(
                        degradation_notice(
                            WarningCode.huazi_animation_fallback,
                            f"{len(animation_fallbacks)} 条花字动画不在白名单，已按无动画渲染。",
                            node_id=ctx.node_run.node_id,
                        )
                    )
                    warnings.append(WarningCode.huazi_animation_fallback)
                caption_display_payload = _caption_display_payload(display)
            bgm_path = None
            bgm_plan = style.get("bgm") if isinstance(style.get("bgm"), dict) else {}
            bgm_asset_id = style.get("bgm_asset_id") or (bgm_plan or {}).get("asset_id")
            if bgm_plan and bgm_plan.get("enabled") and bgm_asset_id:
                bgm_path = ctx.artifact_path(ctx.source_artifact_for_asset(bgm_asset_id))
            output_path = temp_dir / "final.mp4"
            sfx_mix_events: list[SfxMixEvent] = []
            missing_sfx_assets: list[str] = []
            if caption_windows is not None and caption_display_payload is not None:
                sfx_requests = plan_caption_sfx_events(
                    normal_cues=caption_cues,
                    overlay_events=list(display.emphasis_events),
                    duration=duration,
                )
                for request in sfx_requests:
                    asset_id = str(request.get("asset_id") or "")
                    asset = ctx.repository.media_assets.get(asset_id)
                    if asset is None or asset.kind != "sfx" or not asset.usable:
                        missing_sfx_assets.append(asset_id)
                        continue
                    try:
                        source = ctx.source_artifact_for_asset(asset_id)
                        source_path = ctx.artifact_path(source)
                    except Exception:
                        missing_sfx_assets.append(asset_id)
                        continue
                    sfx_mix_events.append(
                        SfxMixEvent(
                            path=Path(source_path),
                            start_ms=int(request.get("start_ms") or 0),
                            volume=float(request.get("volume") or 0.5),
                            asset_id=asset_id,
                        )
                    )
            if missing_sfx_assets:
                missing_unique = sorted(set(missing_sfx_assets))
                warnings.append(WarningCode.sfx_asset_missing)
                degradations.append(
                    degradation_notice(
                        WarningCode.sfx_asset_missing,
                        "部分字幕音效资产缺失；对应字幕已无声继续。",
                        node_id=ctx.node_run.node_id,
                        affects_true_yield=False,
                    ).model_copy(
                        update={
                            "details": {
                                "asset_ids": missing_unique,
                                "event_count": len(missing_sfx_assets),
                            }
                        }
                    )
                )
            # auto_mix consumed here: LUFS-targeted volume + sidechain ducking +
            # fades when enabled (no longer a dead end-to-end flag).
            auto_mix = bool((bgm_plan or {}).get("auto_mix", state.request.bgm.auto_mix))
            bgm_source_start = _float_or_zero((bgm_plan or {}).get("source_start"))
            bgm_source_end = _float_or_none((bgm_plan or {}).get("source_end"))
            burn_subtitle_path = subtitle_path
            burn_fonts_dir = fonts_dir if resolved_font or resolved_emphasis_font else None
            if subtitle_path is not None and not ffmpeg_filter_available("subtitles"):
                degradations.append(
                    degradation_notice(
                        WarningCode.subtitle_burn_skipped,
                        "当前 ffmpeg 缺少 subtitles/libass filter，已保留字幕文件但未烧录进视频。",
                        node_id=ctx.node_run.node_id,
                    )
                )
                warnings.append(WarningCode.subtitle_burn_skipped)
                burn_subtitle_path = None
                burn_fonts_dir = None
            render_kwargs = {
                "rendered_path": ctx.artifact_path(rendered),
                "audio_path": ctx.artifact_path(audio),
                "output_path": output_path,
                "subtitle_path": burn_subtitle_path,
                "bgm_path": bgm_path,
                "bgm_volume": float((bgm_plan or {}).get("volume", state.request.bgm.volume)),
                "duration": duration,
                "fps": fps,
                "fonts_dir": burn_fonts_dir,
                "auto_mix": auto_mix,
                "bgm_source_start": bgm_source_start,
                "bgm_source_end": bgm_source_end,
                "sfx_events": sfx_mix_events,
            }
            try:
                mix_result = render_final_media(**render_kwargs)
            except FfmpegCommandError:
                if not sfx_mix_events:
                    raise
                warnings.append(WarningCode.sfx_mix_failed)
                degradations.append(
                    degradation_notice(
                        WarningCode.sfx_mix_failed,
                        "字幕音效混音失败；已显式降级为无音效成片。",
                        node_id=ctx.node_run.node_id,
                        affects_true_yield=False,
                    ).model_copy(
                        update={"details": {"event_count": len(sfx_mix_events)}}
                    )
                )
                render_kwargs["sfx_events"] = []
                mix_result = render_final_media(**render_kwargs)
            # No silent fallback: when auto-mix wanted LUFS targeting but the
            # loudness probe failed, the mixer quietly used the requested volume.
            # Surface that so the user knows the auto-balance was not applied.
            if (
                mix_result is not None
                and mix_result.metadata.get("fallback_reason") == "loudness_probe_failed"
            ):
                degradations.append(
                    degradation_notice(
                        WarningCode.bgm_loudness_probe_failed,
                        "BGM 响度探测失败，已按请求音量混音（未做自动响度对齐）。",
                        node_id=ctx.node_run.node_id,
                    )
                )
                warnings.append(WarningCode.bgm_loudness_probe_failed)
            media_info = validate_rendered_output(
                output_path,
                expected_frames=total_frames,
                frame_count_message="Final video frame count does not match the timeline.",
            )
            if probe_audio_channels(output_path) != 2:
                raise FfmpegCommandError("Final mixed audio must be stereo.")
            final_stored = store_file(ctx.object_store(), output_path, purpose="generated-video")
            if subtitle_path is not None:
                subtitle_stored = store_file(ctx.object_store(), subtitle_path, purpose="subtitles")
                subtitle_artifact = ctx.artifact(
                    ArtifactKind.subtitle_ass,
                    None,
                    "uri-only",
                    uri=subtitle_stored.ref.uri,
                    sha256=subtitle_stored.sha256,
                    media_info=probe_media(subtitle_path),
                )
    except FfmpegCommandError as exc:
        code = (
            ErrorCode.render_subtitle_failed if state.request.subtitle.enabled else exc.error_code
        )
        raise NodeExecutionError(code, "Subtitle/BGM mix rendering failed.") from exc
    final = ctx.artifact(
        ArtifactKind.video_final,
        None,
        "uri-only",
        uri=final_stored.ref.uri,
        sha256=final_stored.sha256,
        media_info=media_info,
    )
    artifacts = [final]
    if subtitle_artifact is not None:
        artifacts.append(subtitle_artifact)
    if caption_display_payload is not None:
        artifacts.append(
            ctx.artifact(
                ArtifactKind.plan_caption_display,
                caption_display_payload,
                "CaptionDisplayPlan.v1",
            )
        )
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=artifacts,
        warnings=warnings,
        degradations=degradations,
    )


def _caption_display_payload(display: CaptionDisplayResult) -> dict:
    """Serialize the compiler result into the ``CaptionDisplayPlan.v1`` payload.

    Emphasis events are already OverlayEvent-shaped dicts (from ``plan_style``),
    passed through unchanged.
    """

    def cue(item) -> dict:
        return {
            "start": item.start,
            "end": item.end,
            "lines": list(item.lines),
            "source_unit_ids": list(item.source_unit_ids),
            "suppressed_by": item.suppressed_by,
        }

    diag = display.diagnostics
    return {
        "policy_version": "caption_display_v2",
        "normal_cues": [cue(item) for item in display.normal_cues],
        "suppressed_cues": [cue(item) for item in display.suppressed_cues],
        "emphasis_events": [dict(event) for event in display.emphasis_events],
        "diagnostics": {
            "merged_units": diag.merged_units,
            "split_cues": diag.split_cues,
            "suppressed_duplicates": diag.suppressed_duplicates,
            "dropped_fragments": diag.dropped_fragments,
            "animation_fallbacks": diag.animation_fallbacks,
            "font_metrics_source": diag.font_metrics_source,
        },
    }


def _float_or_zero(value) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None
