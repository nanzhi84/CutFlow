"""BrollRenderBase node: render the B-roll-only base video."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.rendering import render_broll_montage, validate_rendered_output
from packages.media.video.ffmpeg import FfmpegCommandError
from packages.production._broll_overlays import broll_overlays_from_plan
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._timeline_grid import to_frame


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    render_plan = state.require(ArtifactKind.plan_render).payload or {}
    timeline = state.require(ArtifactKind.plan_timeline).payload or {}
    broll_plan = state.require(ArtifactKind.plan_broll).payload or {}
    render_size = render_plan.get(
        "render_size",
        [state.request.output.width, state.request.output.height],
    )
    width = int(render_size[0])
    height = int(render_size[1])
    fps = int(render_plan.get("fps") or state.request.output.fps)
    total_frames = int(timeline.get("total_frames") or 0)
    if total_frames <= 0:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Render plan has no frames.")
    broll_segments = []
    for overlay in broll_overlays_from_plan(broll_plan):
        segment_frames = overlay.model_dump()
        segment_frames["timeline_start_frame"] = to_frame(overlay.timeline_start, fps)
        segment_frames["timeline_end_frame"] = to_frame(overlay.timeline_end, fps)
        segment_frames["source_start_frame"] = to_frame(overlay.source_start, fps)
        segment_frames["source_end_frame"] = to_frame(overlay.source_end, fps)
        broll_segments.append(segment_frames)

    try:
        with tempfile.TemporaryDirectory(prefix="cutagent-broll-render-") as directory:
            output_path = Path(directory) / "rendered.mp4"
            render_broll_montage(
                segments=broll_segments,
                output_path=output_path,
                total_frames=total_frames,
                width=width,
                height=height,
                fps=fps,
                source_artifact_for_asset=ctx.source_artifact_for_asset,
                artifact_path=ctx.artifact_path,
            )
            media_info = validate_rendered_output(
                output_path,
                expected_frames=total_frames,
                expected_width=width,
                expected_height=height,
                expected_fps=fps,
            )
            stored = store_file(
                ctx.object_store(),
                output_path,
                purpose="generated-video",
                tier="ephemeral",
            )
    except FfmpegCommandError as exc:
        raise NodeExecutionError(exc.error_code, "B-roll base rendering failed.") from exc

    artifact = ctx.artifact(
        ArtifactKind.video_rendered,
        None,
        "uri-only",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=media_info,
    )
    return NodeOutput(artifacts=[artifact])
