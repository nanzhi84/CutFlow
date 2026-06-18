"""RenderFinalTimeline node: composite the lipsync track + b-roll overlays."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.rendering import render_video_timeline, validate_rendered_output
from packages.media.video.ffmpeg import FfmpegCommandError
from packages.production.pipeline._node_context import NodeContext


def _broll_segment_index(segment_id: str | None, fallback: int) -> int:
    if segment_id and segment_id.startswith("broll_"):
        suffix = segment_id.removeprefix("broll_")
        if suffix.isdigit():
            return max(0, int(suffix) - 1)
    return fallback


def _broll_segments_from_timeline(timeline: dict, broll_plan: dict, fps: int) -> list[dict]:
    plan_segments = list(broll_plan.get("segments", []))
    tracks = [
        track
        for track in timeline.get("tracks", [])
        if track.get("track_id") == "broll"
    ]
    tracks.sort(key=lambda track: int(track.get("timeline_start_frame") or 0))

    rendered_segments: list[dict] = []
    for fallback_index, track in enumerate(tracks):
        plan_index = _broll_segment_index(track.get("segment_id"), fallback_index)
        original = dict(plan_segments[plan_index]) if plan_index < len(plan_segments) else {}
        start_frame = int(track.get("timeline_start_frame") or 0)
        end_frame = int(track.get("timeline_end_frame") or 0)
        source_start_frame = track.get("source_start_frame")
        source_end_frame = track.get("source_end_frame")
        if source_start_frame is None:
            source_start_frame = round(float(original.get("source_start", 0) or 0) * fps)
        if source_end_frame is None:
            source_end_frame = round(float(original.get("source_end", 0) or 0) * fps)
        original.update(
            {
                "start_sec": start_frame / fps,
                "end_sec": end_frame / fps,
                "source_start": int(source_start_frame) / fps,
                "source_end": int(source_end_frame) / fps,
            }
        )
        rendered_segments.append(original)
    return rendered_segments


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
                broll_segments=_broll_segments_from_timeline(timeline, broll_plan, fps),
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
