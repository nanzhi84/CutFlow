"""SubtitleAndBgmMix node: burn subtitles, mix voice + BGM into the final video."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media, probe_video_frame_count
from packages.production.pipeline._ffmpeg import render_final_media
from packages.production.pipeline._fonts import ResolvedFont, resolve_subtitle_font
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._subtitles import write_ass_subtitles

# Sentinel font asset id meaning "no real font selected" (StylePlanning emits it
# with a font.default_used warning); it has no uploaded file to register.
_DEFAULT_FONT_SENTINEL = "case_default_font"


def _resolve_selected_font(ctx: NodeContext, style: dict, runtime_dir) -> ResolvedFont | None:
    """Stage the user/agent-selected subtitle font for libass, or None to default.

    Reads the resolved font asset id from the style plan, loads its source file,
    copies it into ``runtime_dir`` and derives the family name so the ASS style
    burns the chosen font instead of silently falling back to Arial.
    """
    subtitle = style.get("subtitle") if isinstance(style.get("subtitle"), dict) else {}
    font = style.get("font") if isinstance(style.get("font"), dict) else {}
    font_asset_id = (
        style.get("font_asset_id")
        or (font or {}).get("font_id")
        or (subtitle or {}).get("font_id")
    )
    if not font_asset_id or font_asset_id == _DEFAULT_FONT_SENTINEL:
        return None
    try:
        font_artifact = ctx.source_artifact_for_asset(font_asset_id)
        font_path = ctx.artifact_path(font_artifact)
    except Exception:  # noqa: BLE001 - missing font asset must degrade, not crash
        return None
    asset = ctx.repository.media_assets.get(font_asset_id)
    fallback_name = getattr(asset, "title", None) if asset is not None else None
    return resolve_subtitle_font(
        font_path=font_path,
        runtime_dir=runtime_dir,
        fallback_name=fallback_name,
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
    try:
        with tempfile.TemporaryDirectory(prefix="cutagent-final-") as directory:
            temp_dir = Path(directory)
            subtitle_path = temp_dir / "subtitle.ass" if state.request.subtitle.enabled else None
            resolved_font: ResolvedFont | None = None
            if subtitle_path is not None:
                resolved_font = _resolve_selected_font(ctx, style, temp_dir / "fonts")
                write_ass_subtitles(
                    subtitle_path,
                    narration=narration,
                    style=style,
                    width=state.request.output.width,
                    height=state.request.output.height,
                    font_name=resolved_font.family_name if resolved_font else None,
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
            render_final_media(
                rendered_path=ctx.artifact_path(rendered),
                audio_path=ctx.artifact_path(audio),
                output_path=output_path,
                subtitle_path=subtitle_path,
                bgm_path=bgm_path,
                bgm_volume=float((bgm_plan or {}).get("volume", state.request.bgm.volume)),
                duration=duration,
                fps=fps,
                fonts_dir=resolved_font.fonts_dir if resolved_font else None,
                auto_mix=auto_mix,
            )
            media_info = probe_media(output_path)
            if probe_video_frame_count(output_path) != total_frames:
                raise NodeExecutionError(
                    ErrorCode.render_invalid_timeline,
                    "Final video frame count does not match the timeline.",
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
        code = ErrorCode.render_subtitle_failed if state.request.subtitle.enabled else exc.error_code
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
    return NodeOutput(artifacts=artifacts)
