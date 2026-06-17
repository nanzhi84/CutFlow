from __future__ import annotations

from pathlib import Path

import pytest

from packages.core.contracts import ErrorCode, MediaInfo
from packages.core.workflow import NodeExecutionError
from packages.media.rendering import validate_rendered_output
import packages.media.rendering.timeline as rendering_timeline


def _video_info(*, width: int = 720, height: int = 1280, fps: float = 30.0) -> MediaInfo:
    return MediaInfo(
        media_type="video",
        codec="h264",
        format="mp4",
        width=width,
        height=height,
        fps=fps,
        duration_sec=1.0,
    )


def test_validate_rendered_output_returns_media_info_when_render_matches(monkeypatch):
    path = Path("rendered.mp4")
    media_info = _video_info()
    monkeypatch.setattr(rendering_timeline, "probe_media", lambda actual: media_info)
    monkeypatch.setattr(rendering_timeline, "probe_video_frame_count", lambda actual: 30)

    result = validate_rendered_output(
        path,
        expected_frames=30,
        expected_width=720,
        expected_height=1280,
        expected_fps=30,
    )

    assert result is media_info


def test_validate_rendered_output_raises_render_error_for_frame_mismatch(monkeypatch):
    monkeypatch.setattr(rendering_timeline, "probe_media", lambda _path: _video_info())
    monkeypatch.setattr(rendering_timeline, "probe_video_frame_count", lambda _path: 29)

    with pytest.raises(NodeExecutionError) as exc_info:
        validate_rendered_output(
            Path("final.mp4"),
            expected_frames=30,
            frame_count_message="Final video frame count does not match the timeline.",
        )

    assert exc_info.value.error.code == ErrorCode.render_invalid_timeline
    assert exc_info.value.error.message == "Final video frame count does not match the timeline."


def test_validate_rendered_output_raises_render_error_for_media_info_mismatch(monkeypatch):
    monkeypatch.setattr(rendering_timeline, "probe_media", lambda _path: _video_info(width=640))
    monkeypatch.setattr(rendering_timeline, "probe_video_frame_count", lambda _path: 30)

    with pytest.raises(NodeExecutionError) as exc_info:
        validate_rendered_output(
            Path("rendered.mp4"),
            expected_frames=30,
            expected_width=720,
            expected_height=1280,
            expected_fps=30,
        )

    assert exc_info.value.error.code == ErrorCode.render_invalid_timeline
    assert exc_info.value.error.message == "Rendered timeline media info does not match the plan."
