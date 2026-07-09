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
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media
from packages.production.pipeline._ffmpeg import render_final_media
from packages.production.pipeline._fonts import ResolvedFont, resolve_subtitle_font
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline._subtitles import write_ass_subtitles

# Sentinel font asset id meaning "no real font selected" (StylePlanning emits it
# with a font.default_used warning); it has no uploaded file to register.
_DEFAULT_FONT_SENTINEL = "case_default_font"


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


def _resolve_font_asset(
    ctx: NodeContext, font_asset_id: str | None, runtime_dir
) -> tuple[ResolvedFont | None, str | None]:
    """Stage the user/agent-selected subtitle font for libass, or None to default.

    Loads the resolved font asset id, copies it into ``runtime_dir`` and derives
    the family name so the ASS style burns the chosen font instead of silently
    falling back to Arial.

    Returns ``(resolved_font, unresolved_font_asset_id)``. The second element is
    the requested font asset id when a font WAS selected but its file could not be
    staged — so the caller emits a ``font.resolution_failed`` degradation instead
    of silently defaulting to Arial. It is ``None`` when no font was selected or
    the selected font resolved fine.
    """
    if not font_asset_id or font_asset_id == _DEFAULT_FONT_SENTINEL:
        return None, None
    try:
        font_artifact = ctx.source_artifact_for_asset(font_asset_id)
        font_path = ctx.artifact_path(font_artifact)
    except Exception:
        return None, font_asset_id
    asset = ctx.repository.media_assets.get(font_asset_id)
    fallback_name = getattr(asset, "title", None) if asset is not None else None
    return (
        resolve_subtitle_font(
            font_path=font_path,
            runtime_dir=runtime_dir,
            fallback_name=fallback_name,
        ),
        None,
    )


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    rendered = state.require(ArtifactKind.video_rendered)
    audio = state.require(ArtifactKind.audio_tts)
    timeline = state.require(ArtifactKind.plan_timeline).payload or {}
    style = state.require(ArtifactKind.plan_style).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    fps = int(timeline.get("fps") or state.request.output.fps)
    total_frames = int(timeline.get("total_frames") or 0)
    duration = total_frames / fps if total_frames else float(rendered.media_info.duration_sec or 0)
    subtitle_artifact = None
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
                resolved_font, unresolved_font_id = _resolve_font_asset(
                    ctx, normal_font_asset_id, fonts_dir
                )
                unresolved_emphasis_font_id: str | None = None
                if emphasis_font_asset_id and emphasis_font_asset_id != normal_font_asset_id:
                    resolved_emphasis_font, unresolved_emphasis_font_id = _resolve_font_asset(
                        ctx, emphasis_font_asset_id, fonts_dir
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
                write_ass_subtitles(
                    subtitle_path,
                    narration=narration,
                    style=style,
                    width=state.request.output.width,
                    height=state.request.output.height,
                    font_name=resolved_font.family_name if resolved_font else None,
                    emphasis_font_name=(
                        resolved_emphasis_font.family_name if resolved_emphasis_font else None
                    ),
                    overlay_events=style.get("overlay_events"),
                )
            bgm_path = None
            bgm_plan = style.get("bgm") if isinstance(style.get("bgm"), dict) else {}
            bgm_asset_id = style.get("bgm_asset_id") or (bgm_plan or {}).get("asset_id")
            if bgm_plan and bgm_plan.get("enabled") and bgm_asset_id:
                bgm_path = ctx.artifact_path(ctx.source_artifact_for_asset(bgm_asset_id))
            output_path = temp_dir / "final.mp4"
            # auto_mix consumed here: LUFS-targeted volume + sidechain ducking +
            # fades when enabled (no longer a dead end-to-end flag).
            auto_mix = bool((bgm_plan or {}).get("auto_mix", state.request.bgm.auto_mix))
            bgm_source_start = _float_or_zero((bgm_plan or {}).get("source_start"))
            bgm_source_end = _float_or_none((bgm_plan or {}).get("source_end"))
            render_kwargs = {
                "rendered_path": ctx.artifact_path(rendered),
                "audio_path": ctx.artifact_path(audio),
                "output_path": output_path,
                "subtitle_path": subtitle_path,
                "bgm_path": bgm_path,
                "bgm_volume": float((bgm_plan or {}).get("volume", state.request.bgm.volume)),
                "duration": duration,
                "fps": fps,
                "fonts_dir": fonts_dir if resolved_font or resolved_emphasis_font else None,
                "auto_mix": auto_mix,
                "bgm_source_start": bgm_source_start,
                "bgm_source_end": bgm_source_end,
            }
            try:
                mix_result = render_final_media(**render_kwargs)
            except FfmpegCommandError as exc:
                if subtitle_path is None or not _subtitle_filter_unavailable(exc):
                    raise
                degradations.append(
                    degradation_notice(
                        WarningCode.subtitle_burn_skipped,
                        "当前 ffmpeg 缺少 subtitles/libass filter，已保留字幕文件但未烧录进视频。",
                        node_id=ctx.node_run.node_id,
                    )
                )
                warnings.append(WarningCode.subtitle_burn_skipped)
                render_kwargs["subtitle_path"] = None
                render_kwargs["fonts_dir"] = None
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
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=artifacts,
        warnings=warnings,
        degradations=degradations,
    )


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


def _subtitle_filter_unavailable(exc: FfmpegCommandError) -> bool:
    stderr = exc.stderr or ""
    command = " ".join(str(part) for part in exc.command)
    return "subtitles" in command and (
        "No such filter: 'subtitles'" in stderr or "Filter not found" in stderr
    )
