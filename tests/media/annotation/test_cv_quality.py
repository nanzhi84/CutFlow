from __future__ import annotations

from packages.media.annotation import detect_cv_quality_events
from packages.media.annotation.sensors.cv_quality import (
    merge_blur_segments,
    parse_blackdetect,
    parse_freezedetect,
)
from tests.media.annotation.fixtures import make_black_video


def test_detect_cv_quality_flags_black_on_black_clip(tmp_path):
    video = make_black_video(tmp_path, duration=1.5)

    events = detect_cv_quality_events(video, black_min_dur=0.3)

    # A fully black clip must yield at least one hard occlusion event covering it.
    occlusions = [e for e in events if e["event_type"] == "occlusion"]
    assert occlusions, f"expected occlusion events, got {events}"
    ev = occlusions[0]
    assert ev["risk_tier"] == "hard"
    assert ev["source"] == "sensor"
    assert ev["start"] < 0.2
    assert ev["end"] > 1.0


def test_detect_cv_quality_missing_file_returns_empty():
    assert detect_cv_quality_events("/nonexistent.mp4") == []


def test_parse_blackdetect_pairs_start_and_end():
    text = (
        "[blackdetect @ 0x1] black_start:1 black_end:1.96 black_duration:0.96\n"
        "[blackdetect @ 0x1] black_start:3.0 black_end:3.5\n"
    )
    assert parse_blackdetect(text) == [(1.0, 1.96), (3.0, 3.5)]


def test_parse_blackdetect_unclosed_uses_total_duration():
    text = "[blackdetect @ 0x1] black_start:2.0\n"
    assert parse_blackdetect(text, total_duration=5.0) == [(2.0, 5.0)]
    # No basis to close -> dropped.
    assert parse_blackdetect(text, total_duration=0.0) == []


def test_parse_freezedetect_state_machine():
    text = (
        "lavfi.freezedetect.freeze_start: 0\n"
        "lavfi.freezedetect.freeze_duration: 1\n"
        "lavfi.freezedetect.freeze_end: 1\n"
    )
    assert parse_freezedetect(text) == [(0.0, 1.0)]


def test_merge_blur_segments_merges_contiguous_low_variance():
    # Uniform 0.5s frame step; variance 10 (blur) at t=0,0.5,1.0 then 200 (sharp).
    times = [0.0, 0.5, 1.0, 1.5, 2.0]
    variances = [10.0, 10.0, 10.0, 200.0, 200.0]
    segs = merge_blur_segments(times, variances, threshold=60.0, min_dur=0.4)
    assert len(segs) == 1
    start, end = segs[0]
    assert start == 0.0
    # last blur frame (1.0) + one median step (0.5) = 1.5.
    assert abs(end - 1.5) < 1e-6


def test_merge_blur_segments_threshold_is_strict_and_drops_short():
    # Exactly at threshold is not blur.
    assert merge_blur_segments([0.0, 1.0], [60.0, 60.0], threshold=60.0, min_dur=0.1) == []
    # Length mismatch -> defensive empty.
    assert merge_blur_segments([0.0], [10.0, 10.0], threshold=60.0, min_dur=0.1) == []
