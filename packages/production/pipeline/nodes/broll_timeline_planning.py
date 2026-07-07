"""BrollTimelinePlanning node: build a B-roll-only render timeline."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production._broll_overlays import broll_overlays_from_plan
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._timeline_grid import build_tracks, to_frame, validate_timeline
from packages.production.pipeline.nodes._timeline_output import timeline_output


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    repository = ctx.repository
    audio = state.require(ArtifactKind.audio_tts)
    broll_artifact = state.require(ArtifactKind.plan_broll)
    broll = broll_artifact.payload or {}

    duration = float((audio.media_info.duration_sec if audio.media_info else 0) or 0)
    if duration <= 0:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline duration is invalid.")
    fps = int(state.request.output.fps)
    total_frames = max(1, round(duration * fps))

    raw_segments: list[dict] = []
    for index, overlay in enumerate(broll_overlays_from_plan(broll)):
        start_sec = overlay.timeline_start
        end_sec = overlay.timeline_end
        raw_segments.append(
            {
                "track_id": "broll",
                "segment_id": f"broll_{index + 1}",
                "asset_ref": repository.artifact_ref(broll_artifact.id),
                "start_sec": start_sec,
                "end_sec": end_sec,
                "source_start_sec": overlay.source_start,
                "source_end_sec": overlay.source_end or end_sec,
                "timeline_start_frame": to_frame(start_sec, fps),
                "timeline_end_frame": to_frame(end_sec, fps),
                "source_start_frame": None,
                "source_end_frame": None,
            }
        )

    validation = validate_timeline(raw_segments, fps, total_frames)
    if not validation.valid:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline validation failed.")

    tracks = build_tracks(raw_segments, fps)
    return timeline_output(ctx, fps=fps, total_frames=total_frames, tracks=tracks, validation=validation)
