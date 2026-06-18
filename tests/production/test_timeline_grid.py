from __future__ import annotations

from packages.core.contracts import ArtifactKind, ArtifactRef
from packages.production.pipeline._timeline_grid import (
    align_broll_to_portrait_cuts,
    build_tracks,
    validate_timeline,
)


def _ref(artifact_id: str, kind: ArtifactKind = ArtifactKind.plan_broll) -> ArtifactRef:
    return ArtifactRef(artifact_id=artifact_id, kind=kind, uri=f"artifact://{artifact_id}")


def _segment(
    *,
    track_id: str = "broll",
    segment_id: str = "seg_1",
    start_sec: float = 0.0,
    end_sec: float = 1.0,
    timeline_start_frame: int | None = None,
    timeline_end_frame: int | None = None,
) -> dict:
    return {
        "track_id": track_id,
        "segment_id": segment_id,
        "asset_ref": _ref(f"{track_id}_{segment_id}"),
        "start_sec": start_sec,
        "end_sec": end_sec,
        "source_start_sec": start_sec,
        "source_end_sec": end_sec,
        "timeline_start_frame": timeline_start_frame,
        "timeline_end_frame": timeline_end_frame,
        "source_start_frame": None,
        "source_end_frame": None,
    }


def test_build_tracks_prefers_explicit_frame_grid_and_falls_back_to_seconds():
    raw_segments = [
        _segment(
            track_id="portrait",
            segment_id="portrait_1",
            start_sec=0.123,
            end_sec=1.987,
            timeline_start_frame=0,
            timeline_end_frame=50,
        ),
        _segment(track_id="broll", segment_id="broll_1", start_sec=1.0, end_sec=2.0),
    ]

    tracks = build_tracks(raw_segments, fps=25)

    assert tracks[0].track_id == "portrait"
    assert tracks[0].timeline_start_frame == 0
    assert tracks[0].timeline_end_frame == 50
    assert tracks[0].source_start_frame == 3
    assert tracks[0].source_end_frame == 50
    assert tracks[1].track_id == "broll"
    assert tracks[1].timeline_start_frame == 25
    assert tracks[1].timeline_end_frame == 50


def test_align_broll_end_snaps_to_nearby_portrait_cut():
    raw_segments = [
        _segment(
            track_id="portrait",
            segment_id="portrait_1",
            start_sec=0.0,
            end_sec=5.0,
            timeline_start_frame=0,
            timeline_end_frame=150,
        ),
        _segment(
            track_id="portrait",
            segment_id="portrait_2",
            start_sec=5.0,
            end_sec=10.0,
            timeline_start_frame=150,
            timeline_end_frame=300,
        ),
        _segment(
            track_id="broll",
            segment_id="broll_1",
            start_sec=3.0,
            end_sec=4.9,
        ),
    ]

    aligned = align_broll_to_portrait_cuts(raw_segments, fps=30, max_gap_frames=6)
    broll = next(segment for segment in aligned if segment["track_id"] == "broll")

    assert broll["timeline_start_frame"] == 90
    assert broll["timeline_end_frame"] == 150
    assert broll["source_start_frame"] == 90
    assert broll["source_end_frame"] == 150
    assert broll["end_sec"] == 5.0


def test_validate_timeline_reports_valid_grid_for_portrait_and_broll_tracks():
    raw_segments = [
        _segment(
            track_id="portrait",
            segment_id="portrait_1",
            start_sec=0.0,
            end_sec=2.0,
            timeline_start_frame=0,
            timeline_end_frame=50,
        ),
        _segment(track_id="broll", segment_id="broll_1", start_sec=0.5, end_sec=1.5),
        _segment(track_id="broll", segment_id="broll_2", start_sec=1.5, end_sec=2.0),
    ]

    validation = validate_timeline(raw_segments, fps=25, total_frames=50)

    assert validation.valid is True
    assert validation.checks == {
        "overlap": True,
        "negative_duration": True,
        "out_of_bounds": True,
    }


def test_validate_timeline_flags_overlap_per_track():
    raw_segments = [
        _segment(segment_id="broll_1", start_sec=0.0, end_sec=1.2),
        _segment(segment_id="broll_2", start_sec=1.0, end_sec=2.0),
    ]

    validation = validate_timeline(raw_segments, fps=25, total_frames=50)

    assert validation.valid is False
    assert validation.checks["overlap"] is False
    assert validation.checks["negative_duration"] is True
    assert validation.checks["out_of_bounds"] is True


def test_validate_timeline_flags_out_of_bounds():
    raw_segments = [_segment(start_sec=0.0, end_sec=2.1)]

    validation = validate_timeline(raw_segments, fps=25, total_frames=50)

    assert validation.valid is False
    assert validation.checks["out_of_bounds"] is False
    assert validation.checks["overlap"] is True
    assert validation.checks["negative_duration"] is True


def test_validate_timeline_flags_negative_duration():
    raw_segments = [_segment(start_sec=1.0, end_sec=1.0)]

    validation = validate_timeline(raw_segments, fps=25, total_frames=50)

    assert validation.valid is False
    assert validation.checks["negative_duration"] is False
    assert validation.checks["overlap"] is True
    assert validation.checks["out_of_bounds"] is True
