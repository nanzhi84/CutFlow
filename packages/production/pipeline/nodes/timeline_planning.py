"""TimelinePlanning node: build + validate the timeline and render plan."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.contracts.artifacts import BrollOverlay
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._timeline_grid import (
    build_tracks,
    validate_timeline,
)
from packages.production.pipeline._materialize import full_coverage_broll_coverage_gaps
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._broll_policy import broll_full_coverage_enabled
from packages.production.pipeline.nodes._timeline_output import timeline_output


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    repository = ctx.repository
    portrait_artifact = state.require(ArtifactKind.plan_portrait)
    broll_artifact = state.require(ArtifactKind.plan_broll)
    portrait = portrait_artifact.payload or {}
    broll = broll_artifact.payload or {}
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}
    fps = int(windows.get("fps") or portrait.get("fps") or 30)
    duration = float(portrait.get("duration_sec", 0))
    total_frames = max(0, int(windows.get("total_frames") or 0))
    if duration <= 0 and broll_full_coverage_enabled(state.request):
        duration = total_frames / fps if total_frames > 0 else 0
    if duration <= 0:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline duration is invalid.")
    if total_frames <= 0:
        total_frames = max(1, round(duration * fps))
    broll_window_by_id = {
        str(window.get("window_id") or ""): window
        for window in (windows.get("broll_windows") or [])
        if isinstance(window, dict) and window.get("window_id")
    }
    full_coverage_broll = broll_full_coverage_enabled(state.request)

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
    raw_overlays = broll.get("overlays") if isinstance(broll.get("overlays"), list) else []
    for index, item in enumerate(raw_overlays):
        if not isinstance(item, dict):
            continue
        overlay = BrollOverlay.model_validate(item)
        # B-roll boundaries are authoritative frame fields produced from
        # TimelineWindowPlanning's B-roll windows by the shared materializer. This node
        # is verify-only: it never re-snaps to portrait cuts or re-derives frames from
        # seconds. A missing frame is an upstream contract defect -> fail fast.
        missing = [
            name
            for name, value in (
                ("timeline_start_frame", overlay.timeline_start_frame),
                ("timeline_end_frame", overlay.timeline_end_frame),
                ("source_start_frame", overlay.source_start_frame),
                ("source_end_frame", overlay.source_end_frame),
            )
            if value is None
        ]
        if missing:
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                f"B-roll overlay {overlay.overlay_id} is missing authoritative frame "
                f"boundaries: {', '.join(missing)}.",
            )
        if not overlay.window_id:
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                f"B-roll overlay {overlay.overlay_id} is missing authoritative window_id.",
            )
        window = broll_window_by_id.get(overlay.window_id)
        if window is None:
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                f"B-roll overlay {overlay.overlay_id} references unknown authoritative "
                f"window_id '{overlay.window_id}'.",
            )
        expected_start = int(window.get("start_frame", 0) or 0)
        expected_end = int(window.get("end_frame", 0) or 0)
        if full_coverage_broll:
            drifts = (
                overlay.timeline_start_frame < expected_start
                or overlay.timeline_end_frame > expected_end
            )
        else:
            drifts = (
                overlay.timeline_start_frame != expected_start
                or overlay.timeline_end_frame != expected_end
            )
        if drifts:
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                f"B-roll overlay {overlay.overlay_id} drifts from authoritative window "
                f"'{overlay.window_id}': expected frames {expected_start}-{expected_end}, "
                f"got {overlay.timeline_start_frame}-{overlay.timeline_end_frame}.",
            )
        raw_segments.append(
            {
                "track_id": "broll",
                "segment_id": f"broll_{index + 1}",
                "asset_ref": repository.artifact_ref(broll_artifact.id),
                "start_sec": overlay.timeline_start,
                "end_sec": overlay.timeline_end,
                "source_start_sec": overlay.source_start,
                "source_end_sec": overlay.source_end,
                "timeline_start_frame": overlay.timeline_start_frame,
                "timeline_end_frame": overlay.timeline_end_frame,
                "source_start_frame": overlay.source_start_frame,
                "source_end_frame": overlay.source_end_frame,
                "pad_start": overlay.pad_start,
                "pad_end": overlay.pad_end,
            }
        )

    if full_coverage_broll:
        coverage_gaps = full_coverage_broll_coverage_gaps(
            windows=windows,
            overlays=[item for item in raw_overlays if isinstance(item, dict)],
        )
        if coverage_gaps:
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                "B-roll full coverage overlays leave gaps inside authoritative windows.",
                details={"coverage_gaps": coverage_gaps},
            )

    validation = validate_timeline(raw_segments, fps, total_frames)
    if not validation.valid:
        raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline validation failed.")

    tracks = build_tracks(raw_segments, fps)
    return timeline_output(ctx, fps=fps, total_frames=total_frames, tracks=tracks, validation=validation)
