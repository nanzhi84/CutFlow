"""Pure timeline frame-grid helpers shared by production planning nodes."""

from __future__ import annotations

from packages.core.contracts.artifacts import TimelineTrackSegment, TimelineValidationReport

BROLL_PORTRAIT_CUT_SNAP_MAX_FRAMES = 10


def to_frame(seconds: float, fps: int) -> int:
    return round(seconds * fps)


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
        )
        for segment in raw_segments
    ]


def align_broll_to_portrait_cuts(
    raw_segments: list[dict],
    fps: int,
    *,
    max_gap_frames: int = BROLL_PORTRAIT_CUT_SNAP_MAX_FRAMES,
) -> list[dict]:
    """Extend near-ended b-roll overlays to the next portrait cut frame."""
    if max_gap_frames <= 0:
        return list(raw_segments)

    portrait_cuts = sorted(
        {
            frame
            for segment in raw_segments
            if segment["track_id"] == "portrait"
            for frame in (_timeline_start(segment, fps), _timeline_end(segment, fps))
            if frame > 0
        }
    )
    if not portrait_cuts:
        return list(raw_segments)

    broll_starts_by_index = {
        index: _timeline_start(segment, fps)
        for index, segment in enumerate(raw_segments)
        if segment["track_id"] == "broll"
    }
    broll_indices = sorted(broll_starts_by_index, key=lambda index: broll_starts_by_index[index])
    next_broll_start: dict[int, int] = {}
    for position, index in enumerate(broll_indices[:-1]):
        next_broll_start[index] = broll_starts_by_index[broll_indices[position + 1]]

    aligned: list[dict] = []
    for index, segment in enumerate(raw_segments):
        if segment["track_id"] != "broll":
            aligned.append(segment)
            continue

        start_frame = _timeline_start(segment, fps)
        end_frame = _timeline_end(segment, fps)
        target_cut = next((cut for cut in portrait_cuts if cut > end_frame), None)
        if target_cut is None:
            aligned.append(segment)
            continue

        gap_frames = target_cut - end_frame
        following_broll_start = next_broll_start.get(index)
        if (
            gap_frames <= 0
            or gap_frames > max_gap_frames
            or target_cut <= start_frame
            or (following_broll_start is not None and following_broll_start < target_cut)
        ):
            aligned.append(segment)
            continue

        source_start_frame = _source_start(segment, fps)
        adjusted = dict(segment)
        adjusted["timeline_start_frame"] = start_frame
        adjusted["timeline_end_frame"] = target_cut
        adjusted["source_start_frame"] = source_start_frame
        adjusted["source_end_frame"] = source_start_frame + (target_cut - start_frame)
        adjusted["start_sec"] = round(start_frame / fps, 3)
        adjusted["end_sec"] = round(target_cut / fps, 3)
        adjusted["source_start_sec"] = round(source_start_frame / fps, 3)
        adjusted["source_end_sec"] = round(adjusted["source_end_frame"] / fps, 3)
        aligned.append(adjusted)

    return aligned


def validate_timeline(
    raw_segments: list[dict],
    fps: int,
    total_frames: int,
) -> TimelineValidationReport:
    """Validate overlap, negative duration, and bounds checks per the legacy node."""
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
