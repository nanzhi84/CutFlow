"""PortraitTrackBuild node: transcode + concat portrait segments to one track."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import ArtifactKind, ErrorCode, NodeStatus
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.rendering import (
    concat_video_segments,
    fit_video_to_exact_duration,
    transcode_video_segment,
)
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media
from packages.production.pipeline._timeline_grid import to_frame
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._broll_policy import broll_full_coverage_enabled


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    if broll_full_coverage_enabled(state.request):
        return NodeOutput(status=NodeStatus.skipped)
    portrait = state.require(ArtifactKind.plan_portrait).payload or {}
    duration = float(portrait.get("duration_sec", 0) or 0)
    segments = portrait.get("segments", [])
    if not segments:
        raise NodeExecutionError(ErrorCode.material_insufficient_portrait, "Portrait plan has no segments.")
    fps = int(portrait.get("fps") or state.request.output.fps)
    width = state.request.output.width
    height = state.request.output.height
    try:
        with tempfile.TemporaryDirectory(prefix="cutagent-portrait-") as directory:
            temp_dir = Path(directory)
            segment_paths: list[Path] = []
            for index, segment in enumerate(segments):
                # TimelineWindowPlanning compiles frame-aligned defaults on the 30fps
                # grid, and the final media-selection node preserves those authoritative
                # frame fields in plan.portrait. A missing source frame is therefore an
                # upstream contract defect, not something to silently re-derive from
                # seconds (#105). Fail fast before source resolution / ffmpeg work.
                missing = [
                    name
                    for name in ("source_start_frame", "source_end_frame")
                    if segment.get(name) is None
                ]
                if missing:
                    raise NodeExecutionError(
                        ErrorCode.render_invalid_timeline,
                        f"Portrait segment {index + 1} is missing source frame "
                        f"boundaries: {', '.join(missing)}.",
                    )
                source_start_frame = int(segment["source_start_frame"])
                source_end_frame = int(segment["source_end_frame"])
                source_artifact = ctx.source_artifact_for_asset(segment.get("asset_id"))
                source_path = ctx.artifact_path(source_artifact)
                source_info = source_artifact.media_info or probe_media(source_path)
                source_duration = float(source_info.duration_sec or 0)
                source_duration_frames = to_frame(source_duration, fps)
                if (
                    source_start_frame < 0
                    or source_end_frame <= source_start_frame
                    or source_end_frame > source_duration_frames
                ):
                    raise NodeExecutionError(
                        ErrorCode.render_invalid_timeline,
                        "Portrait source window is out of bounds.",
                    )
                output_path = temp_dir / f"portrait_segment_{index + 1}.mp4"
                transcode_video_segment(
                    source_path,
                    output_path,
                    source_start_frame=source_start_frame,
                    source_end_frame=source_end_frame,
                    width=width,
                    height=height,
                    fps=fps,
                )
                segment_paths.append(output_path)
            raw_track_path = temp_dir / "portrait_track_raw.mp4"
            concat_video_segments(segment_paths, raw_track_path)
            # Per-segment -t ms-quantization + fps resampling + concat (-c copy)
            # accumulate sub-frame drift that exceeds 1/fps for longer tracks.
            # Force the track to be EXACTLY the plan duration (clone-pad if short,
            # trim if long) so the sanity check below passes reliably.
            concat_path = temp_dir / "portrait_track.mp4"
            fit_video_to_exact_duration(
                raw_track_path,
                concat_path,
                duration=duration,
                width=width,
                height=height,
                fps=fps,
            )
            media_info = probe_media(concat_path)
            if abs(float(media_info.duration_sec or 0) - duration) > max(2 / fps, 0.05):
                raise NodeExecutionError(
                    ErrorCode.render_invalid_timeline,
                    "Portrait track duration does not match the plan.",
                )
            stored = store_file(
                ctx.object_store(),
                concat_path,
                purpose="generated-video",
                # Durable (cloud OSS) so the cloud lipsync provider (DashScope
                # VideoReTalk) can download a presigned HTTPS URL of this portrait
                # track. Ephemeral = local MinIO (127.0.0.1), unreachable by the vendor.
                tier="durable",
            )
    except FfmpegCommandError as exc:
        raise NodeExecutionError(exc.error_code, "Portrait track build failed.") from exc
    artifact = ctx.artifact(
        ArtifactKind.video_portrait_track,
        None,
        "uri-only",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=media_info,
    )
    return NodeOutput(artifacts=[artifact])
