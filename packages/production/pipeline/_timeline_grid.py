"""Pure timeline frame-grid helpers shared by production planning nodes."""

from __future__ import annotations

import math

from packages.core.contracts.artifacts import TimelineTrackSegment, TimelineValidationReport


def to_frame(seconds: float, fps: int) -> int:
    # Keep the round-half-up boundary invariant from planning.editing.frame_grid.
    return max(0, int(math.floor(float(seconds) * fps + 0.5)))


def _timeline_start(segment: dict, fps: int) -> int:
    if segment["timeline_start_frame"] is not None:
        return segment["timeline_start_frame"]
    return to_frame(segment["start_sec"], fps)


def _timeline_end(segment: dict, fps: int) -> int:
    if segment["timeline_end_frame"] is not None:
        return segment["timeline_end_frame"]
    return to_frame(segment["end_sec"], fps)


def _source_start(segment: dict, fps: int) -> int:
    if segment["source_start_frame"] is not None:
        return segment["source_start_frame"]
    return to_frame(segment.get("source_start_sec", segment["start_sec"]), fps)


def _source_end(segment: dict, fps: int) -> int:
    if segment["source_end_frame"] is not None:
        return segment["source_end_frame"]
    return to_frame(segment.get("source_end_sec", segment["end_sec"]), fps)


def build_tracks(raw_segments: list[dict], fps: int) -> list[TimelineTrackSegment]:
    return [
        TimelineTrackSegment(
            track_id=segment["track_id"],
            segment_id=segment["segment_id"],
            asset_ref=segment["asset_ref"],
            timeline_start_frame=_timeline_start(segment, fps),
            timeline_end_frame=_timeline_end(segment, fps),
            source_start_frame=_source_start(segment, fps),
            source_end_frame=_source_end(segment, fps),
            pad_start=float(segment.get("pad_start", 0.0) or 0.0),
            pad_end=float(segment.get("pad_end", 0.0) or 0.0),
            fade_frames=segment.get("fade_frames"),
            placement=segment.get("placement"),
        )
        for segment in raw_segments
    ]


def validate_timeline(
    raw_segments: list[dict],
    fps: int,
    total_frames: int,
) -> TimelineValidationReport:
    """Validate overlap, negative duration, and bounds checks."""
    negative_duration = any(
        _timeline_end(segment, fps) <= _timeline_start(segment, fps) for segment in raw_segments
    )
    out_of_bounds = any(
        _timeline_start(segment, fps) < 0 or _timeline_end(segment, fps) > total_frames
        for segment in raw_segments
    )
    overlap = False
    by_track: dict[str, list[dict]] = {}
    for segment in raw_segments:
        by_track.setdefault(segment["track_id"], []).append(segment)
    for segments in by_track.values():
        ordered = sorted(segments, key=lambda segment: _timeline_start(segment, fps))
        previous_end = None
        for segment in ordered:
            if previous_end is not None and _timeline_start(segment, fps) < previous_end:
                overlap = True
            segment_end = _timeline_end(segment, fps)
            previous_end = max(previous_end or segment_end, segment_end)
    return TimelineValidationReport(
        valid=not (negative_duration or out_of_bounds or overlap),
        checks={
            "overlap": not overlap,
            "negative_duration": not negative_duration,
            "out_of_bounds": not out_of_bounds,
        },
    )
