"""TimelinePlanning node: build + validate the timeline and render plan."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.contracts.artifacts import (
    RenderPlanArtifact,
    TimelinePlanArtifact,
    TimelineTrackSegment,
    TimelineValidationReport,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext


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

    def to_frame(seconds: float) -> int:
        return round(seconds * fps)

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
            }
        )

    def timeline_start(segment: dict) -> int:
        if segment["timeline_start_frame"] is not None:
            return segment["timeline_start_frame"]
        return to_frame(segment["start_sec"])

    def timeline_end(segment: dict) -> int:
        if segment["timeline_end_frame"] is not None:
            return segment["timeline_end_frame"]
        return to_frame(segment["end_sec"])

    def source_start(segment: dict) -> int:
        if segment["source_start_frame"] is not None:
            return segment["source_start_frame"]
        return to_frame(segment.get("source_start_sec", segment["start_sec"]))

    def source_end(segment: dict) -> int:
        if segment["source_end_frame"] is not None:
            return segment["source_end_frame"]
        return to_frame(segment.get("source_end_sec", segment["end_sec"]))

    negative_duration = any(timeline_end(segment) <= timeline_start(segment) for segment in raw_segments)
    out_of_bounds = any(
        timeline_start(segment) < 0 or timeline_end(segment) > total_frames
        for segment in raw_segments
    )
    overlap = False
    by_track: dict[str, list[dict]] = {}
    for segment in raw_segments:
        by_track.setdefault(segment["track_id"], []).append(segment)
    for segments in by_track.values():
        ordered = sorted(segments, key=timeline_start)
        previous_end = None
        for segment in ordered:
            if previous_end is not None and timeline_start(segment) < previous_end:
                overlap = True
            previous_end = max(previous_end or timeline_end(segment), timeline_end(segment))
    if negative_duration or out_of_bounds or overlap:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline validation failed.")

    tracks = [
        TimelineTrackSegment(
            track_id=segment["track_id"],
            segment_id=segment["segment_id"],
            asset_ref=segment["asset_ref"],
            timeline_start_frame=timeline_start(segment),
            timeline_end_frame=timeline_end(segment),
            source_start_frame=source_start(segment),
            source_end_frame=source_end(segment),
        )
        for segment in raw_segments
    ]
    validation = TimelineValidationReport(
        valid=True,
        checks={
            "overlap": not overlap,
            "negative_duration": not negative_duration,
            "out_of_bounds": not out_of_bounds,
        },
    )
    timeline = TimelinePlanArtifact(
        fps=fps,
        total_frames=total_frames,
        tracks=tracks,
        validation=validation,
    )
    render_plan = RenderPlanArtifact(
        timeline_artifact_id="pending",
        render_size=(state.request.output.width, state.request.output.height),
        fps=fps,
        tracks=tracks,
    )
    timeline_artifact = ctx.artifact(
        ArtifactKind.plan_timeline,
        timeline.model_dump(mode="json"),
        "TimelinePlanArtifact.v1",
    )
    render_plan = render_plan.model_copy(update={"timeline_artifact_id": timeline_artifact.id})
    return NodeOutput(
        artifacts=[
            timeline_artifact,
            ctx.artifact(
                ArtifactKind.plan_render,
                render_plan.model_dump(mode="json"),
                "RenderPlanArtifact.v1",
            ),
        ]
    )
