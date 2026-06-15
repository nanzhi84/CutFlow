from __future__ import annotations

import hashlib

import pytest

from packages.core.contracts import MediaInfo
from packages.media.video.ffmpeg import (
    FfmpegCommandError,
    compress_video_to_budget,
    extract_frame_at_time,
    extract_thumbnails,
    needs_normalize_for_upload,
    normalize_for_upload,
    probe_media,
    sha256_file,
    stabilize_video,
    trim_to_valid_segments,
)
from tests.fixtures.media import generate_test_audio, generate_test_video


def test_probe_media_reads_real_video_stream_info(tmp_path):
    video = generate_test_video(tmp_path, duration_sec=2, width=320, height=568, fps=30)

    info = probe_media(video)

    assert isinstance(info, MediaInfo)
    assert info.media_type == "video"
    assert info.format
    assert info.codec
    assert info.width == 320
    assert info.height == 568
    assert info.fps == 30
    assert 1.9 <= (info.duration_sec or 0) <= 2.2
    assert sha256_file(video) == hashlib.sha256(video.read_bytes()).hexdigest()


def test_probe_media_reads_real_audio_stream_info(tmp_path):
    audio = generate_test_audio(tmp_path, duration_sec=1.5, sample_rate=16000)

    info = probe_media(audio)

    assert info.media_type == "audio"
    assert info.codec
    assert info.format
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert 1.4 <= (info.duration_sec or 0) <= 1.7


def test_extract_thumbnails_writes_first_and_midpoint_pngs(tmp_path):
    video = generate_test_video(tmp_path, duration_sec=2, width=320, height=568, fps=30)
    output_dir = tmp_path / "thumbs"

    thumbs = extract_thumbnails(video, output_dir, labels=("first", "mid"))

    assert [thumb.label for thumb in thumbs] == ["first", "mid"]
    assert all(thumb.path.exists() for thumb in thumbs)
    assert all(thumb.sha256 == sha256_file(thumb.path) for thumb in thumbs)
    assert all(thumb.media_info.media_type == "image" for thumb in thumbs)
    assert all(thumb.media_info.width == 320 for thumb in thumbs)
    assert all(thumb.media_info.height == 568 for thumb in thumbs)


def test_stabilize_video_writes_valid_video_with_matching_duration(tmp_path):
    video = generate_test_video(tmp_path, duration_sec=1.2, width=160, height=120, fps=15)

    stabilized = stabilize_video(video)

    assert stabilized.exists()
    assert stabilized != video
    original_info = probe_media(video)
    stabilized_info = probe_media(stabilized)
    assert stabilized_info.media_type == "video"
    assert stabilized_info.width == original_info.width
    assert stabilized_info.height == original_info.height
    assert abs((stabilized_info.duration_sec or 0) - (original_info.duration_sec or 0)) <= 0.25
    assert sha256_file(stabilized) != sha256_file(video)


def test_trim_to_valid_segments_writes_valid_video_with_expected_duration(tmp_path):
    video = generate_test_video(tmp_path, duration_sec=2, width=160, height=120, fps=15)

    trimmed = trim_to_valid_segments(
        video,
        [
            {"start_sec": 0.2, "end_sec": 0.7},
            {"start_sec": 1.1, "end_sec": 1.6},
        ],
    )

    info = probe_media(trimmed)
    assert trimmed.exists()
    assert info.media_type == "video"
    assert info.width == 160
    assert info.height == 120
    assert 0.8 <= (info.duration_sec or 0) <= 1.25


def test_trim_to_valid_segments_rejects_out_of_bounds_windows(tmp_path):
    video = generate_test_video(tmp_path, duration_sec=1, width=160, height=120, fps=15)

    try:
        trim_to_valid_segments(video, [{"start_sec": 0.2, "end_sec": 1.4}])
    except FfmpegCommandError as exc:
        assert exc.error_code.value == "render.invalid_timeline"
    else:
        raise AssertionError("trim_to_valid_segments should reject out-of-bounds segments")


def test_compress_video_to_budget_reduces_file_below_cap(tmp_path):
    # A high-bitrate source we then squeeze under a small byte budget.
    video = generate_test_video(tmp_path, duration_sec=2, width=640, height=480, fps=30)
    source_size_mb = video.stat().st_size / (1024 * 1024)

    # Pick a budget below the source so at least one strategy must engage.
    budget_mb = max(0.05, source_size_mb * 0.5)
    result = compress_video_to_budget(video, max_size_mb=budget_mb)

    assert result.path.exists()
    assert result.path != video
    assert result.size_bytes <= budget_mb * 1024 * 1024
    assert result.media_info.media_type == "video"
    assert result.strategy in {"reduce_bitrate", "720p", "480p"}


def test_compress_video_to_budget_uses_resolution_ladder_for_tiny_budget(tmp_path):
    # A 720p source whose bitrate floor at full resolution overshoots a small budget,
    # so the ladder must downscale to reach it.
    video = generate_test_video(tmp_path, duration_sec=3, width=1280, height=720, fps=30)

    result = compress_video_to_budget(video, max_size_mb=0.5)

    assert result.size_bytes <= 0.5 * 1024 * 1024
    # Reaching the budget required a resolution-reduction rung, not bitrate alone.
    assert result.strategy in {"720p", "480p"}
    assert (result.media_info.width or 0) < 1280


def test_compress_video_to_budget_raises_typed_error_when_unachievable(tmp_path):
    video = generate_test_video(tmp_path, duration_sec=2, width=640, height=480, fps=30)

    # A budget no encode can hit -> typed render_failed, not a silent None.
    with pytest.raises(FfmpegCommandError) as exc:
        compress_video_to_budget(video, max_size_mb=0.0005)
    assert exc.value.error_code.value == "render.failed"


def test_compress_video_to_budget_rejects_non_video(tmp_path):
    audio = generate_test_audio(tmp_path, duration_sec=1.0, sample_rate=16000)

    with pytest.raises(FfmpegCommandError) as exc:
        compress_video_to_budget(audio, max_size_mb=10)
    assert exc.value.error_code.value == "render.failed"


def test_extract_frame_at_time_writes_single_clamped_frame(tmp_path):
    video = generate_test_video(tmp_path, duration_sec=2, width=320, height=568, fps=30)
    output = tmp_path / "frame.png"

    # A timestamp beyond the duration is clamped into range rather than failing.
    result = extract_frame_at_time(video, output, time_sec=99.0)

    assert output.exists()
    assert result.sha256 == sha256_file(output)
    assert result.media_info.media_type == "image"
    assert result.media_info.width == 320
    assert result.media_info.height == 568


def test_extract_frame_at_time_rejects_non_video(tmp_path):
    audio = generate_test_audio(tmp_path, duration_sec=1, sample_rate=16000)
    try:
        extract_frame_at_time(audio, tmp_path / "frame.png", time_sec=0.5)
    except FfmpegCommandError:
        pass
    else:
        raise AssertionError("extract_frame_at_time should reject non-video sources")


def test_normalize_for_upload_produces_h264_aac_mp4(tmp_path):
    video = generate_test_video(tmp_path, duration_sec=1, width=160, height=120, fps=15)
    output = tmp_path / "normalized.mp4"

    normalize_for_upload(video, output)

    assert output.exists()
    info = probe_media(output)
    assert info.media_type == "video"
    assert info.codec.lower() in {"h264", "avc1"}
    # Re-normalized output is already platform-compliant.
    assert needs_normalize_for_upload(output) is False


def test_session_media_fixture_factory_caches_generated_assets(media_fixture_factory):
    first = media_fixture_factory.video(duration_sec=1, width=320, height=568, fps=30)
    second = media_fixture_factory.video(duration_sec=1, width=320, height=568, fps=30)
    audio = media_fixture_factory.audio(duration_sec=1, sample_rate=16000)

    assert first == second
    assert first.exists()
    assert audio.exists()
