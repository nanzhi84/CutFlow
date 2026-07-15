"""Burn the authoritative caption composition and mix voice, BGM and optional SFX."""

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
from packages.core.contracts.artifacts import CaptionCompositionPlanArtifact
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.rendering import validate_rendered_output
from packages.media.video.ffmpeg import FfmpegCommandError, probe_audio_channels, probe_media
from packages.production.pipeline._ffmpeg import (
    SfxMixEvent,
    ffmpeg_filter_available,
    render_final_media,
)
from packages.production.pipeline._font_metrics import load_font_metrics
from packages.production.pipeline._fonts import ResolvedFont, is_font_collection, resolve_font_asset
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline._sfx_events import plan_emphasis_sfx_events
from packages.production.pipeline._subtitles import write_ass_subtitles

_CAPTION_EMPHASIS_SFX_TAGS = frozenset(
    {"caption_emphasis", "caption-emphasis", "light_pop", "light-pop"}
)


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    rendered = state.require(ArtifactKind.video_rendered)
    audio = state.require(ArtifactKind.audio_tts)
    timeline = state.require(ArtifactKind.plan_timeline).payload or {}
    style = state.require(ArtifactKind.plan_style).payload or {}
    try:
        composition = CaptionCompositionPlanArtifact.model_validate(
            state.require(ArtifactKind.plan_caption_composition).payload
        ).model_dump(mode="json")
    except Exception as exc:
        raise NodeExecutionError(
            ErrorCode.render_subtitle_failed,
            f"Caption composition artifact is invalid: {exc}",
            retryable=False,
        ) from exc
    fps = int(timeline.get("fps") or state.request.output.fps)
    total_frames = int(timeline.get("total_frames") or 0)
    duration = total_frames / fps if total_frames else float(rendered.media_info.duration_sec or 0)
    subtitle_artifact = None
    degradations: list[DegradationNotice] = []
    warnings: list[WarningCode] = []
    try:
        with tempfile.TemporaryDirectory(prefix="cutagent-final-") as directory:
            temp_dir = Path(directory)
            subtitle_path = temp_dir / "subtitle.ass" if composition["normal_enabled"] else None
            fonts_dir = temp_dir / "fonts"
            resolved_font: ResolvedFont | None = None
            resolved_emphasis_font: ResolvedFont | None = None
            if subtitle_path is not None:
                resolved_font = _resolve_planned_font(
                    ctx,
                    font_asset_id=composition.get("normal_font_asset_id"),
                    runtime_dir=fonts_dir,
                    label="普通字幕",
                )
                if composition.get("emphasis_enabled"):
                    emphasis_id = composition.get("emphasis_font_asset_id")
                    resolved_emphasis_font = (
                        resolved_font
                        if emphasis_id == composition.get("normal_font_asset_id")
                        else _resolve_planned_font(
                            ctx,
                            font_asset_id=emphasis_id,
                            runtime_dir=fonts_dir,
                            label="强调字幕",
                        )
                    )
                else:
                    resolved_emphasis_font = resolved_font
                write_ass_subtitles(
                    subtitle_path,
                    style=style,
                    width=state.request.output.width,
                    height=state.request.output.height,
                    caption_composition=composition,
                    font_name=resolved_font.family_name,
                    emphasis_font_name=resolved_emphasis_font.family_name,
                )

            bgm_path = None
            bgm_plan = style.get("bgm") if isinstance(style.get("bgm"), dict) else {}
            bgm_asset_id = style.get("bgm_asset_id") or (bgm_plan or {}).get("asset_id")
            if bgm_plan and bgm_plan.get("enabled") and bgm_asset_id:
                bgm_path = ctx.artifact_path(ctx.source_artifact_for_asset(bgm_asset_id))

            sfx_mix_events: list[SfxMixEvent] = []
            selected_sfx_id = _select_emphasis_sfx_asset_id(
                ctx.repository.media_assets.values()
            )
            for request in plan_emphasis_sfx_events(
                caption_composition=composition,
                duration=duration,
                sfx_asset_id=selected_sfx_id,
            ):
                asset_id = str(request["asset_id"])
                try:
                    source_path = ctx.artifact_path(ctx.source_artifact_for_asset(asset_id))
                except Exception:
                    warnings.append(WarningCode.sfx_asset_missing)
                    degradations.append(
                        degradation_notice(
                            WarningCode.sfx_asset_missing,
                            f"强调字幕音效资产（{asset_id}）无法读取；已无声继续。",
                            node_id=ctx.node_run.node_id,
                            affects_true_yield=False,
                        )
                    )
                    continue
                sfx_mix_events.append(
                    SfxMixEvent(
                        path=Path(source_path),
                        start_ms=int(request["start_ms"]),
                        volume=float(request["volume"]),
                        asset_id=asset_id,
                    )
                )

            output_path = temp_dir / "final.mp4"
            burn_subtitle_path = subtitle_path
            burn_fonts_dir = fonts_dir if resolved_font else None
            if subtitle_path is not None and not ffmpeg_filter_available("subtitles"):
                warnings.append(WarningCode.subtitle_burn_skipped)
                degradations.append(
                    degradation_notice(
                        WarningCode.subtitle_burn_skipped,
                        "当前 ffmpeg 缺少 subtitles/libass filter，已保留字幕文件但未烧录。",
                        node_id=ctx.node_run.node_id,
                    )
                )
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
                "auto_mix": bool((bgm_plan or {}).get("auto_mix", state.request.bgm.auto_mix)),
                "bgm_source_start": _float_or_zero((bgm_plan or {}).get("source_start")),
                "bgm_source_end": _float_or_none((bgm_plan or {}).get("source_end")),
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
                        "强调字幕音效混音失败；已显式降级为无音效成片。",
                        node_id=ctx.node_run.node_id,
                        affects_true_yield=False,
                    ).model_copy(update={"details": {"event_count": len(sfx_mix_events)}})
                )
                render_kwargs["sfx_events"] = []
                mix_result = render_final_media(**render_kwargs)
            if (
                mix_result is not None
                and mix_result.metadata.get("fallback_reason") == "loudness_probe_failed"
            ):
                warnings.append(WarningCode.bgm_loudness_probe_failed)
                degradations.append(
                    degradation_notice(
                        WarningCode.bgm_loudness_probe_failed,
                        "BGM 响度探测失败，已按请求音量混音。",
                        node_id=ctx.node_run.node_id,
                    )
                )
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
        code = ErrorCode.render_subtitle_failed if composition["normal_enabled"] else exc.error_code
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


def _select_emphasis_sfx_asset_id(assets) -> str | None:
    """Select only an explicitly tagged light caption-emphasis sound."""

    for asset in sorted(assets, key=lambda item: item.id):
        tags = {str(tag).strip().lower() for tag in asset.tags}
        if (
            asset.kind == "sfx"
            and asset.usable
            and tags.intersection(_CAPTION_EMPHASIS_SFX_TAGS)
        ):
            return str(asset.id)
    return None


def _resolve_planned_font(
    ctx: NodeContext,
    *,
    font_asset_id: str | None,
    runtime_dir: Path,
    label: str,
) -> ResolvedFont:
    resolved, unresolved = resolve_font_asset(
        font_asset_id=font_asset_id,
        runtime_dir=runtime_dir,
        source_artifact_for_asset=ctx.source_artifact_for_asset,
        artifact_path=ctx.artifact_path,
    )
    if unresolved or resolved is None:
        raise NodeExecutionError(
            ErrorCode.render_subtitle_failed,
            f"字幕计划使用的{label}字体资产（{unresolved or font_asset_id}）无法加载。",
            retryable=False,
        )
    if is_font_collection(resolved) or load_font_metrics(resolved.source_path) is None:
        raise NodeExecutionError(
            ErrorCode.render_subtitle_failed,
            f"字幕计划使用的{label}字体资产（{font_asset_id}）缺少唯一可读字形度量。",
            retryable=False,
        )
    return resolved


def _float_or_zero(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value: object) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None
