from __future__ import annotations

import pytest

from packages.media.video.ffmpeg import (
    FfmpegCommandError,
    extract_thumbnails,
    normalize_for_upload,
    probe_media,
    stabilize_video,
)
from tests.fixtures.media import (
    generate_test_hdr_video,
    generate_test_video,
    require_ffmpeg_filters,
    require_strict_bt709_tags,
)


def _make_hdr(tmp_path):
    """Build an HDR fixture, skipping when the encoder isn't available."""
    try:
        video = generate_test_hdr_video(tmp_path, duration_sec=1, width=320, height=568, fps=15)
    except FfmpegCommandError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"HDR fixture unavailable (no libx265 / 10-bit support): {exc}")
    info = probe_media(video)
    if not info.is_hdr:  # pragma: no cover - environment-dependent
        pytest.skip("ffmpeg did not tag the fixture as HDR; cannot exercise tonemap path")
    return video


def test_normalize_for_upload_tonemaps_hdr_to_bt709_1080p(tmp_path):
    require_ffmpeg_filters("zscale")
    require_strict_bt709_tags()
    video = _make_hdr(tmp_path)

    result = normalize_for_upload(video, tmp_path / "hdr_normalized.mp4")

    assert result.is_hdr is True  # source was HDR
    assert result.output_path.exists()
    # Portrait source -> 1080x1920 strict profile.
    assert (result.target_width, result.target_height) == (1080, 1920)
    info = result.media_info
    assert info.media_type == "video"
    assert info.codec == "h264"
    assert info.width == 1080
    assert info.height == 1920
    # Post-encode validate gate guarantees BT.709 SDR output (no silent degrade).
    assert info.is_hdr is False
    assert info.color_transfer == "bt709"
    assert info.color_primaries == "bt709"


def test_normalize_for_upload_normalizes_sdr_to_strict_profile(tmp_path):
    require_strict_bt709_tags()
    # An odd-resolution SDR source must be scaled/padded to 1080p bt709 h264.
    video = generate_test_video(tmp_path, duration_sec=1, width=300, height=540, fps=15)

    result = normalize_for_upload(video, tmp_path / "sdr_normalized.mp4")

    assert result.is_hdr is False
    info = result.media_info
    assert (info.width, info.height) == (1080, 1920)
    assert info.codec == "h264"
    assert info.color_transfer == "bt709"
    assert info.color_primaries == "bt709"


def test_normalize_for_upload_rejects_non_video(tmp_path):
    from tests.fixtures.media import generate_test_audio

    audio = generate_test_audio(tmp_path, duration_sec=1, sample_rate=16000)

    with pytest.raises(FfmpegCommandError) as excinfo:
        normalize_for_upload(audio, tmp_path / "out.mp4")
    assert excinfo.value.error_code.value == "upload.unsupported_type"


def test_extract_thumbnails_tonemaps_hdr_source(tmp_path):
    require_ffmpeg_filters("zscale")
    video = _make_hdr(tmp_path)

    thumbs = extract_thumbnails(video, tmp_path / "hdr_thumbs", labels=("first", "mid"))

    assert [t.label for t in thumbs] == ["first", "mid"]
    assert all(t.path.exists() for t in thumbs)
    assert all(t.media_info.media_type == "image" for t in thumbs)
    # Thumbnail keeps source pixel dimensions (no rescale in thumbnail step).
    assert all(t.media_info.width == 320 and t.media_info.height == 568 for t in thumbs)


def test_stabilize_video_tonemaps_hdr_source_to_bt709(tmp_path):
    require_ffmpeg_filters("zscale", "vidstabdetect", "vidstabtransform")
    require_strict_bt709_tags()
    video = _make_hdr(tmp_path)

    stabilized = stabilize_video(video, tmp_path / "hdr_stabilized.mp4")

    info = probe_media(stabilized)
    assert info.media_type == "video"
    # Stabilized HDR output is tonemapped to BT.709 SDR, not left HDR.
    assert info.is_hdr is False
    assert info.color_transfer == "bt709"
