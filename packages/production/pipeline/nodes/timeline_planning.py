"""TimelinePlanning node: build + validate the timeline and render plan."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._timeline_grid import (
    align_broll_to_portrait_cuts,
    build_tracks,
    validate_timeline,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._timeline_output import timeline_output


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    repository = ctx.repository
    portrait_artifact = state.require(ArtifactKind.plan_portrait)
    broll_artifact = state.require(ArtifactKind.plan_broll)
    portrait = portrait_artifact.payload or {}
    broll = broll_artifact.payload or {}
    duration = float(portrait.get("duration_sec", 0))
    if duration <= 0:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline duration is invalid.")
    fps = int(portrait.get("fps") or 30)
    total_frames = max(1, round(duration * fps))

    raw_segments: list[dict] = []
    for index, segment in enumerate(portrait.get("segments", [])):
        # The portrait planner emits exact frame indices; trust them verbatim so the
        # contiguous frame grid survives untouched (fall back to seconds otherwise).
        start_frame = segment.get("timeline_start_frame")
        end_frame = segment.get("timeline_end_frame")
        source_start_frame = segment.get("source_start_frame")
        source_end_frame = segment.get("source_end_frame")
        raw_segments.append(
            {
                "track_id": "portrait",
                "segment_id": f"portrait_{index + 1}",
                "asset_ref": repository.artifact_ref(portrait_artifact.id),
                "start_sec": float(segment.get("start_sec", 0)),
                "end_sec": float(segment.get("end_sec", duration)),
                "source_start_sec": float(segment.get("source_start", 0)),
                "source_end_sec": float(segment.get("source_end", segment.get("end_sec", duration))),
                "timeline_start_frame": int(start_frame) if start_frame is not None else None,
                "timeline_end_frame": int(end_frame) if end_frame is not None else None,
                "source_start_frame": int(source_start_frame) if source_start_frame is not None else None,
                "source_end_frame": int(source_end_frame) if source_end_frame is not None else None,
                "pad_start": float(segment.get("pad_start", 0) or 0),
                "pad_end": float(segment.get("pad_end", 0) or 0),
            }
        )
    for index, segment in enumerate(broll.get("segments", [])):
        raw_segments.append(
            {
                "track_id": "broll",
                "segment_id": f"broll_{index + 1}",
                "asset_ref": repository.artifact_ref(broll_artifact.id),
                "start_sec": float(segment.get("start_sec", 0)),
                "end_sec": float(segment.get("end_sec", 0)),
                "source_start_sec": float(segment.get("source_start", 0)),
                "source_end_sec": float(segment.get("source_end", segment.get("end_sec", 0))),
                "timeline_start_frame": None,
                "timeline_end_frame": None,
                "source_start_frame": None,
                "source_end_frame": None,
                "pad_start": float(segment.get("pad_start", 0) or 0),
                "pad_end": float(segment.get("pad_end", 0) or 0),
            }
        )

    raw_segments = align_broll_to_portrait_cuts(raw_segments, fps)
    validation = validate_timeline(raw_segments, fps, total_frames)
    if not validation.valid:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline validation failed.")

    tracks = build_tracks(raw_segments, fps)
    return timeline_output(ctx, fps=fps, total_frames=total_frames, tracks=tracks, validation=validation)
