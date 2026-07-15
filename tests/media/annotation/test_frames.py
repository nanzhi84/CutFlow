from __future__ import annotations

import cv2  # type: ignore

from packages.media.annotation import extract_frame_at_time, extract_frames_for_times
from packages.media.annotation.sensors.frames import _build_downscale_filter
from packages.media.annotation.sensors.frames import extract_frames_for_indices
from tests.media.annotation.fixtures import make_multi_cut_video


def test_extract_frame_at_time_writes_downscaled_jpg(tmp_path):
    video = make_multi_cut_video(tmp_path, seg_dur=2.0, width=640, height=480)
    out = tmp_path / "nested" / "frame.jpg"

    ok = extract_frame_at_time(video, 1.0, str(out), max_long_side=320)

    assert ok is True
    assert out.exists()
    img = cv2.imread(str(out))
    assert img is not None
    # Long side capped at 320 (only shrink).
    assert max(img.shape[0], img.shape[1]) <= 320


def test_extract_frames_for_times_returns_existing_frames(tmp_path):
    video = make_multi_cut_video(tmp_path, seg_dur=2.0)
    temp_dir = tmp_path / "frames"

    frames = extract_frames_for_times(
        video, [0.5, 2.5, 4.5], temp_dir=str(temp_dir), max_long_side=256
    )

    assert len(frames) == 3
    for t, path in frames:
        assert isinstance(t, float)
        assert cv2.imread(path) is not None


def test_extract_frames_for_indices_deduplicates_and_preserves_exact_order(tmp_path):
    video = make_multi_cut_video(tmp_path, seg_dur=2.0)

    frames = extract_frames_for_indices(
        video,
        [90, 0, 30, 30],
        temp_dir=str(tmp_path / "batch-frames"),
        max_long_side=256,
    )

    assert [index for index, _path in frames] == [0, 30, 90]
    assert all(cv2.imread(path) is not None for _index, path in frames)


def test_downscale_filter_is_aspect_preserving_even():
    vf = _build_downscale_filter(1024)
    assert "scale=" in vf
    assert "min(iw,1024)" in vf
    assert "min(ih,1024)" in vf
