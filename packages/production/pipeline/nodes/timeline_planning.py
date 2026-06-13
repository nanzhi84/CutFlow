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
    fps = 30
    total_frames = max(1, round(duration * fps))

    def to_frame(seconds: float) -> int:
        return round(seconds * fps)

    raw_segments: list[dict] = []
    for index, segment in enumerate(portrait.get("segments", [])):
        raw_segments.append(
            {
                "track_id": "portrait",
                "segment_id": f"portrait_{index + 1}",
                "asset_ref": repository.artifact_ref(portrait_artifact.id),
                "start_sec": float(segment.get("start_sec", 0)),
                "end_sec": float(segment.get("end_sec", duration)),
                "source_start_sec": float(segment.get("source_start", 0)),
                "source_end_sec": float(segment.get("source_end", segment.get("end_sec", duration))),
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
            }
        )

    negative_duration = any(segment["end_sec"] <= segment["start_sec"] for segment in raw_segments)
    out_of_bounds = any(
        segment["start_sec"] < 0 or to_frame(segment["end_sec"]) > total_frames
        for segment in raw_segments
    )
    overlap = False
    by_track: dict[str, list[dict]] = {}
    for segment in raw_segments:
        by_track.setdefault(segment["track_id"], []).append(segment)
    for segments in by_track.values():
        ordered = sorted(segments, key=lambda item: item["start_sec"])
        previous_end = None
        for segment in ordered:
            if previous_end is not None and segment["start_sec"] < previous_end:
                overlap = True
            previous_end = max(previous_end or segment["end_sec"], segment["end_sec"])
    if negative_duration or out_of_bounds or overlap:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline validation failed.")

    tracks = [
        TimelineTrackSegment(
            track_id=segment["track_id"],
            segment_id=segment["segment_id"],
            asset_ref=segment["asset_ref"],
            timeline_start_frame=to_frame(segment["start_sec"]),
            timeline_end_frame=to_frame(segment["end_sec"]),
            source_start_frame=to_frame(segment.get("source_start_sec", segment["start_sec"])),
            source_end_frame=to_frame(segment.get("source_end_sec", segment["end_sec"])),
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
