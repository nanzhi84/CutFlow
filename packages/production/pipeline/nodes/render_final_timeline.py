"""RenderFinalTimeline node: composite the lipsync track + b-roll overlays."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media, probe_video_frame_count
from packages.production.pipeline._ffmpeg import render_video_timeline
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    lipsync = state.require(ArtifactKind.video_lipsync)
    render_plan = state.require(ArtifactKind.plan_render).payload or {}
    timeline = state.require(ArtifactKind.plan_timeline).payload or {}
    broll_plan = state.require(ArtifactKind.plan_broll).payload or {}
    render_size = render_plan.get("render_size", [state.request.output.width, state.request.output.height])
    width = int(render_size[0])
    height = int(render_size[1])
    fps = int(render_plan.get("fps") or state.request.output.fps)
    total_frames = int(timeline.get("total_frames") or 0)
    if total_frames <= 0:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Render plan has no frames.")
    try:
        with tempfile.TemporaryDirectory(prefix="cutagent-render-") as directory:
            output_path = Path(directory) / "rendered.mp4"
            render_video_timeline(
                main_path=ctx.artifact_path(lipsync),
                output_path=output_path,
                broll_segments=list(broll_plan.get("segments", [])),
                total_frames=total_frames,
                width=width,
                height=height,
                fps=fps,
                source_artifact_for_asset=ctx.source_artifact_for_asset,
                artifact_path=ctx.artifact_path,
            )
            media_info = probe_media(output_path)
            frame_count = probe_video_frame_count(output_path)
            if frame_count != total_frames:
                raise NodeExecutionError(
                    ErrorCode.render_invalid_timeline,
                    "Rendered timeline frame count does not match the plan.",
                )
            if media_info.width != width or media_info.height != height or round(media_info.fps or 0) != fps:
                raise NodeExecutionError(
                    ErrorCode.render_invalid_timeline,
                    "Rendered timeline media info does not match the plan.",
                )
            stored = store_file(
                ctx.object_store(),
                output_path,
                purpose="generated-video",
                tier="ephemeral",
            )
    except FfmpegCommandError as exc:
        raise NodeExecutionError(exc.error_code, "Final timeline rendering failed.") from exc
    artifact = ctx.artifact(
        ArtifactKind.video_rendered,
        None,
        "uri-only",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=media_info,
    )
    return NodeOutput(artifacts=[artifact])
