"""RenderFinalTimeline node: composite the lipsync track + b-roll overlays."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.rendering import (
    render_broll_montage,
    render_video_timeline,
    validate_rendered_output,
)
from packages.media.video.ffmpeg import FfmpegCommandError
from packages.production._broll_overlays import broll_overlays_from_plan
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._broll_policy import broll_full_coverage_enabled


def _broll_segment_index(segment_id: str | None, fallback: int) -> int:
    if segment_id and segment_id.startswith("broll_"):
        suffix = segment_id.removeprefix("broll_")
        if suffix.isdigit():
            return max(0, int(suffix) - 1)
    return fallback


def _broll_segments_from_timeline(timeline: dict, broll_plan: dict, fps: int) -> list[dict]:
    plan_segments = [overlay.model_dump() for overlay in broll_overlays_from_plan(broll_plan)]
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
        missing_frames = [
            name
            for name in (
                "timeline_start_frame",
                "timeline_end_frame",
                "source_start_frame",
                "source_end_frame",
            )
            if track.get(name) is None
        ]
        if missing_frames:
            segment_id = str(track.get("segment_id") or f"broll_{fallback_index + 1}")
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                f"B-roll timeline track {segment_id} is missing authoritative frame "
                f"boundaries: {', '.join(missing_frames)}.",
            )
        start_frame = int(track["timeline_start_frame"])
        end_frame = int(track["timeline_end_frame"])
        source_start_frame = track.get("source_start_frame")
        source_end_frame = track.get("source_end_frame")
        pad_start = float(track.get("pad_start", original.get("pad_start", 0)) or 0)
        pad_end = float(track.get("pad_end", original.get("pad_end", 0)) or 0)
        source_start_frame = int(source_start_frame)
        source_end_frame = int(source_end_frame)
        original.update(
            {
                "timeline_start_frame": start_frame,
                "timeline_end_frame": end_frame,
                "source_start_frame": source_start_frame,
                "source_end_frame": source_end_frame,
                "start_sec": start_frame / fps,
                "end_sec": end_frame / fps,
                "source_start": source_start_frame / fps,
                "source_end": source_end_frame / fps,
                "pad_start": pad_start,
                "pad_end": pad_end,
                "placement": track.get("placement", original.get("placement")),
            }
        )
        rendered_segments.append(original)
    return rendered_segments


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    lipsync = state.artifacts.get(ArtifactKind.video_lipsync)
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
            broll_segments = _broll_segments_from_timeline(timeline, broll_plan, fps)
            if lipsync is not None:
                render_video_timeline(
                    main_path=ctx.artifact_path(lipsync),
                    output_path=output_path,
                    broll_segments=broll_segments,
                    total_frames=total_frames,
                    width=width,
                    height=height,
                    fps=fps,
                    source_artifact_for_asset=ctx.source_artifact_for_asset,
                    artifact_path=ctx.artifact_path,
                )
            elif broll_full_coverage_enabled(state.request):
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
            else:
                state.require(ArtifactKind.video_lipsync)
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
