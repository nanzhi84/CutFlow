from __future__ import annotations

import inspect
import re
from pathlib import Path

from packages.core.contracts import Artifact, ArtifactKind, MediaInfo
from packages.media.rendering import render_video_timeline, transcode_video_segment, validate_rendered_output
import packages.media.rendering.timeline as rendering_timeline
from packages.production.pipeline._timeline_grid import to_frame


def _video_artifact(asset_id: str, path: Path, *, duration_sec: float = 3.0) -> Artifact:
    return Artifact(
        id=f"art_{asset_id}",
        kind=ArtifactKind.uploaded_file,
        local_path=str(path),
        media_info=MediaInfo(
            media_type="video",
            codec="h264",
            format="mp4",
            width=320,
            height=180,
            fps=30.0,
            duration_sec=duration_sec,
        ),
        payload_schema="UploadedFileArtifact.v1",
        payload=None,
    )


def test_render_video_timeline_uses_frame_boundaries_for_broll_overlay(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}

    def capture_run(self, args):
        captured["args"] = args

    monkeypatch.setattr(rendering_timeline.FfmpegRunner, "run", capture_run)
    artifact = _video_artifact("asset_a", tmp_path / "asset_a.mp4")

    render_video_timeline(
        main_path=tmp_path / "main.mp4",
        output_path=tmp_path / "rendered.mp4",
        broll_segments=[
            {
                "asset_id": "asset_a",
                "source_start": 0.033,
                "source_end": 1.067,
                "source_start_frame": 1,
                "source_end_frame": 32,
                "start_sec": 0.067,
                "end_sec": 1.067,
                "timeline_start_frame": 2,
                "timeline_end_frame": 32,
            }
        ],
        total_frames=90,
        width=160,
        height=90,
        fps=30,
        source_artifact_for_asset=lambda _asset_id: artifact,
        artifact_path=lambda source_artifact: Path(source_artifact.local_path),
    )

    args = captured["args"]
    filter_complex = args[args.index("-filter_complex") + 1]

    assert "trim=start_frame=1:end_frame=32" in filter_complex
    assert "enable='gte(n\\,2)*lt(n\\,32)'" in filter_complex
    assert "between(t," not in filter_complex
    assert re.search(r"trim=start=\d+(?:\.\d+)?", filter_complex) is None


def test_render_video_timeline_pads_short_main_track_to_exact_frames(
    tmp_path,
    media_fixture_factory,
):
    main_path = media_fixture_factory.video(
        duration_sec=1.0,
        width=160,
        height=90,
        fps=30,
        filename="short_main.mp4",
    )
    output_path = tmp_path / "rendered_padded_main.mp4"

    render_video_timeline(
        main_path=main_path,
        output_path=output_path,
        broll_segments=[],
        total_frames=33,
        width=160,
        height=90,
        fps=30,
        source_artifact_for_asset=lambda _asset_id: (_ for _ in ()).throw(AssertionError("unused")),
        artifact_path=lambda source_artifact: Path(source_artifact.local_path),
    )

    validate_rendered_output(
        output_path,
        expected_frames=33,
        expected_width=160,
        expected_height=90,
        expected_fps=30,
    )


def test_render_video_timeline_pads_broll_overlay_to_timeline_window(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}

    def capture_run(self, args):
        captured["args"] = args

    monkeypatch.setattr(rendering_timeline.FfmpegRunner, "run", capture_run)
    artifact = _video_artifact("asset_a", tmp_path / "asset_a.mp4", duration_sec=124.854)

    render_video_timeline(
        main_path=tmp_path / "main.mp4",
        output_path=tmp_path / "rendered.mp4",
        broll_segments=[
            {
                "asset_id": "asset_a",
                "source_start": 20.387,
                "source_end": 23.548,
                "source_start_frame": 612,
                "source_end_frame": 706,
                "start_sec": 55.959,
                "end_sec": 59.12,
                "timeline_start_frame": 1679,
                "timeline_end_frame": 1774,
            }
        ],
        total_frames=2161,
        width=1080,
        height=1920,
        fps=30,
        source_artifact_for_asset=lambda _asset_id: artifact,
        artifact_path=lambda source_artifact: Path(source_artifact.local_path),
    )

    args = captured["args"]
    filter_complex = args[args.index("-filter_complex") + 1]

    assert "trim=start_frame=612:end_frame=706" in filter_complex
    assert "tpad=stop_mode=clone:stop=95,trim=start_frame=0:end_frame=95" in filter_complex
    assert "enable='gte(n\\,1679)*lt(n\\,1774)'" in filter_complex


def test_render_video_timeline_consumes_explicit_broll_head_and_tail_pad(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}

    def capture_run(self, args):
        captured["args"] = args

    monkeypatch.setattr(rendering_timeline.FfmpegRunner, "run", capture_run)
    artifact = _video_artifact("asset_a", tmp_path / "asset_a.mp4", duration_sec=20.0)

    render_video_timeline(
        main_path=tmp_path / "main.mp4",
        output_path=tmp_path / "rendered.mp4",
        broll_segments=[
            {
                "asset_id": "asset_a",
                "source_start": 5.0,
                "source_end": 7.8,
                "source_start_frame": 150,
                "source_end_frame": 234,
                "start_sec": 0.0,
                "end_sec": 3.0,
                "timeline_start_frame": 0,
                "timeline_end_frame": 90,
                "pad_start": 0.1,
                "pad_end": 0.15,
            }
        ],
        total_frames=120,
        width=1080,
        height=1920,
        fps=30,
        source_artifact_for_asset=lambda _asset_id: artifact,
        artifact_path=lambda source_artifact: Path(source_artifact.local_path),
    )

    args = captured["args"]
    filter_complex = args[args.index("-filter_complex") + 1]

    assert "trim=start_frame=150:end_frame=234" in filter_complex
    assert "tpad=start_duration=0.1:start_mode=clone" in filter_complex
    assert "tpad=stop_duration=0.15:stop_mode=clone" in filter_complex
    assert "tpad=stop_mode=clone:stop=90,trim=start_frame=0:end_frame=90" in filter_complex


def test_transcode_video_segment_uses_output_frame_trim_without_input_seek(monkeypatch, tmp_path):
    signature = inspect.signature(transcode_video_segment)
    assert "source_start_frame" in signature.parameters
    assert "source_end_frame" in signature.parameters
    assert "source_start" not in signature.parameters
    assert "duration" not in signature.parameters

    captured: dict[str, list[str]] = {}

    def capture_run(self, args):
        captured["args"] = args

    monkeypatch.setattr(rendering_timeline.FfmpegRunner, "run", capture_run)

    transcode_video_segment(
        tmp_path / "source.mp4",
        tmp_path / "segment.mp4",
        source_start_frame=3,
        source_end_frame=27,
        width=160,
        height=90,
        fps=30,
    )

    args = captured["args"]
    input_index = args.index("-i")
    assert "-ss" not in args[:input_index]
    assert "-t" not in args[:input_index]
    vf = args[args.index("-vf") + 1]
    assert "fps=30,trim=start_frame=3:end_frame=27" in vf
    assert "trim=start=" not in vf


def test_render_video_timeline_broll_overlay_fade_uses_frame_alpha_filters(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}

    def capture_run(self, args):
        captured["args"] = args

    monkeypatch.setattr(rendering_timeline.FfmpegRunner, "run", capture_run)
    artifact = _video_artifact("asset_a", tmp_path / "asset_a.mp4", duration_sec=10.0)

    render_video_timeline(
        main_path=tmp_path / "main.mp4",
        output_path=tmp_path / "rendered.mp4",
        broll_segments=[
            {
                "asset_id": "asset_a",
                "source_start_frame": 30,
                "source_end_frame": 120,
                "timeline_start_frame": 0,
                "timeline_end_frame": 90,
                "fade_frames": 8,
            }
        ],
        total_frames=120,
        width=160,
        height=90,
        fps=30,
        source_artifact_for_asset=lambda _asset_id: artifact,
        artifact_path=lambda source_artifact: Path(source_artifact.local_path),
    )

    args = captured["args"]
    filter_complex = args[args.index("-filter_complex") + 1]

    assert (
        "format=yuva420p,fade=t=in:s=0:n=8:alpha=1,fade=t=out:s=82:n=8:alpha=1,"
        in filter_complex
    )
    assert "between(t," not in filter_complex
    # Fades are frame-quantized (n=/s= integers), never float seconds.
    assert re.search(r"fade=t=(?:in|out):s=\d+\.\d", filter_complex) is None


def test_render_video_timeline_pip_placement_scales_and_positions_overlay(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}

    def capture_run(self, args):
        captured["args"] = args

    monkeypatch.setattr(rendering_timeline.FfmpegRunner, "run", capture_run)
    artifact = _video_artifact("asset_a", tmp_path / "asset_a.mp4", duration_sec=10.0)

    render_video_timeline(
        main_path=tmp_path / "main.mp4",
        output_path=tmp_path / "rendered.mp4",
        broll_segments=[
            {
                "asset_id": "asset_a",
                "source_start_frame": 30,
                "source_end_frame": 120,
                "timeline_start_frame": 300,
                "timeline_end_frame": 390,
                "placement": "pip_fixed",
            }
        ],
        total_frames=600,
        width=1080,
        height=1920,
        fps=30,
        source_artifact_for_asset=lambda _asset_id: artifact,
        artifact_path=lambda source_artifact: Path(source_artifact.local_path),
    )

    args = captured["args"]
    filter_complex = args[args.index("-filter_complex") + 1]

    # 1080x1920 pip geometry: a 452x460 window pinned to the top-right.
    assert (
        "scale=452:460:force_original_aspect_ratio=increase,crop=452:460,setsar=1,"
        in filter_complex
    )
    assert "x=580:y=192:eof_action=pass" in filter_complex


def test_render_video_timeline_injects_output_threads_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("CUTAGENT_FFMPEG_THREADS", "2")
    captured: dict[str, list[str]] = {}

    def capture_run(self, args):
        captured["args"] = list(args)

    monkeypatch.setattr(rendering_timeline.FfmpegRunner, "run", capture_run)

    render_video_timeline(
        main_path=tmp_path / "main.mp4",
        output_path=tmp_path / "rendered.mp4",
        broll_segments=[],
        total_frames=90,
        width=160,
        height=90,
        fps=30,
        source_artifact_for_asset=lambda _asset_id: (_ for _ in ()).throw(AssertionError("unused")),
        artifact_path=lambda source_artifact: Path(source_artifact.local_path),
    )

    args = captured["args"]
    # -threads is present on BOTH the input (decode) and output (encode) side.
    assert args.count("-threads") == 2
    an_index = args.index("-an")
    codec_index = args.index("-c:v")
    assert args[an_index + 1 : codec_index] == ["-threads", "2"]


def test_render_video_timeline_output_args_unchanged_without_thread_cap(monkeypatch, tmp_path):
    monkeypatch.delenv("CUTAGENT_FFMPEG_THREADS", raising=False)
    captured: dict[str, list[str]] = {}

    def capture_run(self, args):
        captured["args"] = list(args)

    monkeypatch.setattr(rendering_timeline.FfmpegRunner, "run", capture_run)

    render_video_timeline(
        main_path=tmp_path / "main.mp4",
        output_path=tmp_path / "rendered.mp4",
        broll_segments=[],
        total_frames=90,
        width=160,
        height=90,
        fps=30,
        source_artifact_for_asset=lambda _asset_id: (_ for _ in ()).throw(AssertionError("unused")),
        artifact_path=lambda source_artifact: Path(source_artifact.local_path),
    )

    args = captured["args"]
    assert "-threads" not in args
    assert args[args.index("-an") + 1] == "-c:v"


def test_to_frame_rounds_half_frames_up():
    assert to_frame(12.5 / 30, 30) == 13
