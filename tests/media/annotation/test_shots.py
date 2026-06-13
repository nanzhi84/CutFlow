from __future__ import annotations

from packages.media.annotation import detect_shot_cuts
from packages.media.annotation.sensors.shots import (
    _compute_min_scene_len_frames,
    _scene_starts_to_cut_times,
)
from tests.media.annotation.fixtures import make_multi_cut_video


def test_detect_shot_cuts_finds_two_cuts_on_three_segment_clip(tmp_path):
    video = make_multi_cut_video(tmp_path, fps=25, seg_dur=2.0)

    cuts = detect_shot_cuts(video, min_scene_len_sec=0.5, threshold=27.0)

    # red->green->blue = exactly two hard cuts at the 2s and 4s boundaries.
    assert len(cuts) == 2
    assert abs(cuts[0] - 2.0) < 0.2
    assert abs(cuts[1] - 4.0) < 0.2
    assert cuts == sorted(cuts)


def test_detect_shot_cuts_missing_file_returns_empty():
    assert detect_shot_cuts("/nonexistent/video.mp4") == []
    assert detect_shot_cuts("") == []


def test_scene_starts_to_cut_times_drops_head_and_dedupes():
    # 0.0 is the head (dropped); near-duplicates collapse; ascending.
    out = _scene_starts_to_cut_times([0.0, 4.0, 2.0, 2.0005, 2.0])
    assert out == [2.0, 4.0]


def test_compute_min_scene_len_frames_handles_illegal_inputs():
    assert _compute_min_scene_len_frames(25.0, 3.0) == 75
    assert _compute_min_scene_len_frames(0.0, 3.0) == 1
    assert _compute_min_scene_len_frames(25.0, 0.0) == 1
    assert _compute_min_scene_len_frames("bad", 3.0) == 1
