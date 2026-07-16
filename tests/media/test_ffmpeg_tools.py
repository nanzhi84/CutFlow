from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from packages.core.contracts import MediaInfo
from packages.core.workflow import ExecutionCancelled, cancellation_scope
from packages.media.rendering import promote_staged_media
from packages.media.video import ffmpeg as ffmpeg_mod
from packages.media.video.ffmpeg import (
    FfmpegCommandError,
    compress_video_to_budget,
    extract_frame_at_time,
    extract_thumbnails,
    normalize_for_upload,
    probe_media,
    sha256_file,
    stabilize_video,
    trim_to_valid_segments,
)
from tests.fixtures.media import (
    generate_test_audio,
    generate_test_video,
    require_ffmpeg_filters,
    require_strict_bt709_tags,
)


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
    require_ffmpeg_filters("vidstabdetect", "vidstabtransform")
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
    require_strict_bt709_tags()
    video = generate_test_video(tmp_path, duration_sec=1, width=160, height=120, fps=15)
    output = tmp_path / "normalized.mp4"

    normalize_for_upload(video, output)

    assert output.exists()
    info = probe_media(output)
    assert info.media_type == "video"
    assert info.codec.lower() in {"h264", "avc1"}


def test_session_media_fixture_factory_caches_generated_assets(media_fixture_factory):
    first = media_fixture_factory.video(duration_sec=1, width=320, height=568, fps=30)
    second = media_fixture_factory.video(duration_sec=1, width=320, height=568, fps=30)
    audio = media_fixture_factory.audio(duration_sec=1, sample_rate=16000)

    assert first == second
    assert first.exists()
    assert audio.exists()


def test_ffmpeg_runner_maps_timeout_and_exit_errors():
    with pytest.raises(FfmpegCommandError) as timeout_exc:
        ffmpeg_mod.FfmpegRunner(timeout_sec=0.05, cancel_grace_sec=0.05).run(
            [sys.executable, "-c", "import time; time.sleep(10)"]
        )
    assert timeout_exc.value.error_code.value == "provider.timeout"

    with pytest.raises(FfmpegCommandError) as failed_exc:
        ffmpeg_mod.FfmpegRunner().run(
            [sys.executable, "-c", "import sys; sys.stderr.write('bad codec'); sys.exit(7)"]
        )
    assert failed_exc.value.error_code.value == "render.failed"
    assert failed_exc.value.returncode == 7
    assert failed_exc.value.stderr == "bad codec"


class _DeadlineCancellationToken:
    def __init__(self, *, delay: float, force: bool) -> None:
        self.deadline = time.monotonic() + delay
        self._force = force

    @property
    def cancelled(self) -> bool:
        return time.monotonic() >= self.deadline

    @property
    def force(self) -> bool:
        return self._force


class _EscalatingCancellationToken:
    def __init__(self, *, cancel_after: float, force_after: float) -> None:
        now = time.monotonic()
        self.cancel_deadline = now + cancel_after
        self.force_deadline = now + force_after

    @property
    def cancelled(self) -> bool:
        return time.monotonic() >= self.cancel_deadline

    @property
    def force(self) -> bool:
        return time.monotonic() >= self.force_deadline


class _FailingCancellationToken:
    def __init__(self, pid_file) -> None:
        self.pid_file = pid_file

    @property
    def cancelled(self) -> bool:
        if self.pid_file.exists():
            raise RuntimeError("cancellation backend failed")
        return False

    @property
    def force(self) -> bool:
        return False


def _wait_for_process_exit(pid: int, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"process {pid} is still alive")


def _process_tree_script(
    pid_file,
    *,
    ignore_sigterm: bool,
    child_ignores_sigterm: bool | None = None,
    child_closes_stdio: bool = False,
) -> str:
    handler = "signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_sigterm else ""
    child_ignores_sigterm = (
        ignore_sigterm if child_ignores_sigterm is None else child_ignores_sigterm
    )
    child_handler = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        if child_ignores_sigterm
        else "import time; time.sleep(30)"
    )
    child_stdio = (
        ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
        if child_closes_stdio
        else ""
    )
    return (
        "import os,signal,subprocess,sys,time;"
        f"{handler}"
        f"child=subprocess.Popen([sys.executable,'-c',{child_handler!r}]{child_stdio});"
        f"open({str(pid_file)!r},'w').write(f'{{os.getpid()}} {{child.pid}}');"
        "time.sleep(30)"
    )


def _assert_process_tree_exited(pid_file) -> None:
    parent_pid, child_pid = (int(value) for value in pid_file.read_text().split())
    _wait_for_process_exit(parent_pid)
    _wait_for_process_exit(child_pid)


def test_ffmpeg_runner_graceful_cancel_terminates_process_group(tmp_path):
    pid_file = tmp_path / "graceful-pids.txt"
    token = _DeadlineCancellationToken(delay=0.2, force=False)

    with cancellation_scope(token), pytest.raises(ExecutionCancelled):
        ffmpeg_mod.FfmpegRunner(timeout_sec=10, cancel_grace_sec=1).run(
            [sys.executable, "-c", _process_tree_script(pid_file, ignore_sigterm=False)]
        )

    _assert_process_tree_exited(pid_file)


def test_ffmpeg_runner_kills_child_after_parent_exits_on_sigterm(tmp_path):
    pid_file = tmp_path / "parent-exits-pids.txt"
    token = _DeadlineCancellationToken(delay=0.2, force=False)

    started = time.monotonic()
    with cancellation_scope(token), pytest.raises(ExecutionCancelled):
        ffmpeg_mod.FfmpegRunner(timeout_sec=10, cancel_grace_sec=0.1).run(
            [
                sys.executable,
                "-c",
                _process_tree_script(
                    pid_file,
                    ignore_sigterm=False,
                    child_ignores_sigterm=True,
                ),
            ]
        )

    assert time.monotonic() - started < 1.0
    _assert_process_tree_exited(pid_file)


def test_ffmpeg_runner_checks_process_group_after_parent_pipes_close(tmp_path):
    pid_file = tmp_path / "closed-pipes-pids.txt"
    token = _DeadlineCancellationToken(delay=0.2, force=False)

    with cancellation_scope(token), pytest.raises(ExecutionCancelled):
        ffmpeg_mod.FfmpegRunner(timeout_sec=10, cancel_grace_sec=0.1).run(
            [
                sys.executable,
                "-c",
                _process_tree_script(
                    pid_file,
                    ignore_sigterm=False,
                    child_ignores_sigterm=True,
                    child_closes_stdio=True,
                ),
            ]
        )

    _assert_process_tree_exited(pid_file)


def test_ffmpeg_runner_force_cancel_kills_and_reaps_process_group(tmp_path):
    pid_file = tmp_path / "pids.txt"
    token = _DeadlineCancellationToken(delay=0.2, force=True)

    started = time.monotonic()
    with cancellation_scope(token), pytest.raises(ExecutionCancelled):
        ffmpeg_mod.FfmpegRunner(timeout_sec=10, cancel_grace_sec=5).run(
            [sys.executable, "-c", _process_tree_script(pid_file, ignore_sigterm=True)]
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    _assert_process_tree_exited(pid_file)


def test_ffmpeg_runner_force_escalation_interrupts_grace_period(tmp_path):
    pid_file = tmp_path / "escalated-pids.txt"
    token = _EscalatingCancellationToken(cancel_after=0.1, force_after=0.3)

    started = time.monotonic()
    with cancellation_scope(token), pytest.raises(ExecutionCancelled):
        ffmpeg_mod.FfmpegRunner(timeout_sec=10, cancel_grace_sec=5).run(
            [sys.executable, "-c", _process_tree_script(pid_file, ignore_sigterm=True)]
        )

    assert time.monotonic() - started < 1.0
    _assert_process_tree_exited(pid_file)


def test_ffmpeg_runner_timeout_kills_and_reaps_process_group(tmp_path):
    pid_file = tmp_path / "timeout-pids.txt"

    with pytest.raises(FfmpegCommandError) as excinfo:
        ffmpeg_mod.FfmpegRunner(timeout_sec=0.2, cancel_grace_sec=0.05).run(
            [sys.executable, "-c", _process_tree_script(pid_file, ignore_sigterm=True)]
        )

    assert excinfo.value.error_code.value == "provider.timeout"
    _assert_process_tree_exited(pid_file)


def test_ffmpeg_runner_reaps_process_group_when_cancellation_check_fails(tmp_path):
    pid_file = tmp_path / "checker-failure-pids.txt"
    token = _FailingCancellationToken(pid_file)

    with cancellation_scope(token), pytest.raises(RuntimeError, match="backend failed"):
        ffmpeg_mod.FfmpegRunner(timeout_sec=10).run(
            [sys.executable, "-c", _process_tree_script(pid_file, ignore_sigterm=True)]
        )

    _assert_process_tree_exited(pid_file)


def test_promote_staged_media_fsyncs_and_removes_part(tmp_path):
    staged = tmp_path / "rendered.part.mp4"
    ready = tmp_path / "rendered.mp4"
    staged.write_bytes(b"validated-media")

    promote_staged_media(staged, ready)

    assert ready.read_bytes() == b"validated-media"
    assert not staged.exists()


def test_probe_media_parses_subtitle_image_and_bad_payloads(tmp_path, monkeypatch):
    media = tmp_path / "clip.srt"
    media.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")

    def install_payload(payload):
        class FakeRunner:
            def run(self, args, **_kwargs):
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

        monkeypatch.setattr(ffmpeg_mod, "FfmpegRunner", lambda *a, **k: FakeRunner())

    install_payload(
        {
            "streams": [{"codec_type": "subtitle", "codec_name": "subrip", "duration": "1.5"}],
            "format": {"format_name": "srt"},
        }
    )
    subtitle = probe_media(media)
    assert subtitle.media_type == "subtitle"
    assert subtitle.duration_sec == 1.5

    image = tmp_path / "cover.png"
    image.write_bytes(b"png")
    install_payload(
        {
            "streams": [{"codec_type": "video", "codec_name": "png", "width": "640", "height": "360"}],
            "format": {"format_name": "image2", "duration": "0"},
        }
    )
    image_info = probe_media(image)
    assert image_info.media_type == "image"
    assert image_info.duration_sec is None
    assert image_info.fps is None

    class BadJsonRunner:
        def run(self, args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="{not-json", stderr="")

    monkeypatch.setattr(ffmpeg_mod, "FfmpegRunner", lambda *a, **k: BadJsonRunner())
    with pytest.raises(FfmpegCommandError, match="invalid JSON"):
        probe_media(image)

    install_payload({"streams": [], "format": {}})
    with pytest.raises(FfmpegCommandError, match="No media streams"):
        probe_media(image)


def test_extract_thumbnails_tonemaps_hdr_sources(tmp_path, monkeypatch):
    video = tmp_path / "hdr.mov"
    video.write_bytes(b"video")
    calls: list[list[str]] = []
    returned_infos = iter(
        [
            MediaInfo(media_type="video", codec="hevc", format="mov", duration_sec=4.0, is_hdr=True),
            MediaInfo(media_type="image", codec="png", format="png", width=320, height=568),
            MediaInfo(media_type="image", codec="png", format="png", width=320, height=568),
        ]
    )
    monkeypatch.setattr(ffmpeg_mod, "probe_media", lambda _path: next(returned_infos))

    class FakeRunner:
        def run(self, args, **_kwargs):
            calls.append(list(args))
            output = ffmpeg_mod.Path(args[-1])
            output.write_bytes(b"frame")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg_mod, "FfmpegRunner", lambda *a, **k: FakeRunner())

    thumbs = extract_thumbnails(video, tmp_path / "thumbs")

    assert [thumb.label for thumb in thumbs] == ["first", "mid"]
    assert all(ffmpeg_mod.HDR_TONEMAP_VF in call for call in calls)
    assert all("-color_trc" in call for call in calls)


def test_stabilize_video_builds_hdr_vidstab_chain(tmp_path, monkeypatch):
    source = tmp_path / "portrait.mov"
    source.write_bytes(b"video")
    commands: list[list[str]] = []

    def fake_probe(path):
        if path == source:
            return MediaInfo(
                media_type="video",
                codec="hevc",
                format="mov",
                duration_sec=2.0,
                width=1080,
                height=1920,
                is_hdr=True,
            )
        return MediaInfo(media_type="video", codec="h264", format="mp4", width=1080, height=1920)

    monkeypatch.setattr(ffmpeg_mod, "probe_media", fake_probe)

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, args, **_kwargs):
            commands.append(list(args))
            joined = " ".join(str(part) for part in args)
            if "vidstabdetect" in joined:
                result_token = joined.split("result='", 1)[1].split("'", 1)[0]
                ffmpeg_mod.Path(result_token).write_text("transforms", encoding="utf-8")
            elif "vidstabtransform" in joined:
                ffmpeg_mod.Path(args[-1]).write_bytes(b"stabilized")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg_mod, "FfmpegRunner", lambda *a, **k: FakeRunner())

    output = stabilize_video(source, tmp_path / "out.mp4")

    assert output == tmp_path / "out.mp4"
    assert len(commands) == 2
    assert "vidstabdetect" in " ".join(commands[0])
    second = " ".join(commands[1])
    assert ffmpeg_mod.HDR_TONEMAP_VF in second
    assert "vidstabtransform" in second
    assert "-color_trc bt709" in second


def test_compress_video_to_budget_retries_ladder_and_portrait_sizes(tmp_path, monkeypatch):
    source = tmp_path / "portrait.mp4"
    source.write_bytes(b"video")
    output = tmp_path / "compressed.mp4"
    commands: list[list[str]] = []

    monkeypatch.setattr(
        ffmpeg_mod,
        "probe_media",
        lambda _path: MediaInfo(
            media_type="video",
            codec="h264",
            format="mp4",
            duration_sec=10.0,
            width=1080,
            height=1920,
        ),
    )

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, args, **_kwargs):
            commands.append(list(args))
            attempt = len(commands)
            if attempt == 1:
                raise FfmpegCommandError("encoder failed")
            if attempt == 2:
                output.write_bytes(b"x" * (1024 * 1024 + 1))
            else:
                output.write_bytes(b"x" * 1024)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg_mod, "FfmpegRunner", lambda *a, **k: FakeRunner())

    result = compress_video_to_budget(source, max_size_mb=1.0, output_path=output)

    assert result.strategy == "480p"
    assert len(commands) == 3
    assert "-s" not in commands[0]
    assert "720x1280" in commands[1]
    assert "480x854" in commands[2]


def test_trim_to_valid_segments_single_window_copies_segment(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        ffmpeg_mod,
        "probe_media",
        lambda _path: MediaInfo(media_type="video", codec="h264", format="mp4", duration_sec=3.0),
    )

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, args, **_kwargs):
            commands.append(list(args))
            ffmpeg_mod.Path(args[-1]).write_bytes(b"segment")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg_mod, "FfmpegRunner", lambda *a, **k: FakeRunner())

    output = trim_to_valid_segments(source, [{"start": 0.2, "end": 0.8}], tmp_path / "trimmed.mp4")

    assert output.read_bytes() == b"segment"
    assert len(commands) == 1
    assert "-f" not in commands[0]


def test_normalize_for_upload_builds_crop_hdr_filter_and_target(tmp_path, monkeypatch):
    source = tmp_path / "wide_hdr.mov"
    source.write_bytes(b"video")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        ffmpeg_mod,
        "probe_media",
        lambda _path: MediaInfo(
            media_type="video",
            codec="hevc",
            format="mov",
            duration_sec=6.0,
            is_hdr=True,
        ),
    )
    monkeypatch.setattr(
        ffmpeg_mod,
        "_probe_video_stream_raw",
        lambda _path: {"width": "1920", "height": "1080", "tags": {"rotate": "0"}},
    )
    monkeypatch.setattr(
        ffmpeg_mod,
        "_detect_embedded_portrait_crop",
        lambda *_args, **_kwargs: {"width": 608, "height": 1080, "x": 656, "y": 0},
    )
    monkeypatch.setattr(
        ffmpeg_mod,
        "_validate_normalized_video",
        lambda path, width, height: MediaInfo(
            media_type="video",
            codec="h264",
            format="mp4",
            width=width,
            height=height,
        ),
    )

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, args, **_kwargs):
            commands.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg_mod, "FfmpegRunner", lambda *a, **k: FakeRunner())

    result = normalize_for_upload(source, tmp_path / "normalized.mp4")

    assert (result.target_width, result.target_height) == (1080, 1920)
    vf = commands[0][commands[0].index("-vf") + 1]
    assert vf.startswith("crop=608:1080:656:0,")
    assert ffmpeg_mod.HDR_TONEMAP_VF in vf
    assert "scale=1080:1920" in vf


def test_validate_normalized_video_rejects_empty_and_bad_profile(tmp_path, monkeypatch):
    missing = tmp_path / "missing.mp4"
    with pytest.raises(FfmpegCommandError) as empty_exc:
        ffmpeg_mod._validate_normalized_video(missing, 1080, 1920)
    assert empty_exc.value.error_code.value == "upload.normalization_failed"

    output = tmp_path / "bad.mp4"
    output.write_bytes(b"x" * 2048)
    monkeypatch.setattr(
        ffmpeg_mod,
        "_probe_video_stream_raw",
        lambda _path: {
            "codec_name": "hevc",
            "pix_fmt": "yuv422p",
            "width": 720,
            "height": 1280,
            "color_space": "bt2020nc",
            "color_transfer": "smpte2084",
            "color_primaries": "bt2020",
        },
    )
    with pytest.raises(FfmpegCommandError) as bad_exc:
        ffmpeg_mod._validate_normalized_video(output, 1080, 1920)
    assert "编码=hevc" in str(bad_exc.value)
    assert "像素格式=yuv422p" in str(bad_exc.value)
    assert "分辨率=720x1280" in str(bad_exc.value)

    monkeypatch.setattr(
        ffmpeg_mod,
        "_probe_video_stream_raw",
        lambda _path: {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "width": 1080,
            "height": 1920,
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
        },
    )
    monkeypatch.setattr(
        ffmpeg_mod,
        "probe_media",
        lambda _path: MediaInfo(media_type="video", codec="h264", format="mp4", width=1080, height=1920),
    )
    assert ffmpeg_mod._validate_normalized_video(output, 1080, 1920).codec == "h264"


def test_probe_count_stream_types_and_upload_helpers(tmp_path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    payloads = iter(
        [
            {"streams": [{"nb_read_frames": "", "nb_frames": "42"}]},
            {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}, {"codec_type": ""}]},
            {"streams": [{"codec_type": "video"}]},
            {"streams": [{"nb_read_frames": "", "nb_frames": ""}]},
        ]
    )

    class FakeRunner:
        def run(self, args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(next(payloads)), stderr="")

    monkeypatch.setattr(ffmpeg_mod, "FfmpegRunner", lambda *a, **k: FakeRunner())
    assert ffmpeg_mod.probe_video_frame_count(media) == 42
    assert ffmpeg_mod.probe_stream_types(media) == {"video", "audio"}
    with pytest.raises(FfmpegCommandError, match="Could not count frames"):
        ffmpeg_mod.probe_video_frame_count(media)


def test_cropdetect_and_embedded_portrait_detection(monkeypatch, tmp_path):
    source = tmp_path / "wide.mp4"
    source.write_bytes(b"video")

    monkeypatch.setattr(
        ffmpeg_mod.FfmpegRunner,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stderr="crop=600:1080:660:0\ncrop=608:1078:656:2",
        ),
    )
    assert ffmpeg_mod._run_cropdetect(source, 0.5) == {
        "width": 608,
        "height": 1078,
        "x": 656,
        "y": 2,
    }

    monkeypatch.setattr(
        ffmpeg_mod,
        "_run_cropdetect",
        lambda _source, _time: {"width": 607, "height": 1079, "x": 657, "y": 1},
    )
    assert ffmpeg_mod._detect_embedded_portrait_crop(source, 1920, 1080, 4.0) == {
        "width": 608,
        "height": 1080,
        "x": 656,
        "y": 0,
    }

    monkeypatch.setattr(
        ffmpeg_mod,
        "_run_cropdetect",
        lambda _source, time: {"width": 300 if time < 1 else 900, "height": 1080, "x": 100, "y": 0},
    )
    assert ffmpeg_mod._detect_embedded_portrait_crop(source, 1920, 1080, 4.0) is None
    assert ffmpeg_mod._detect_embedded_portrait_crop(source, 1080, 1920, 4.0) is None


def test_rotation_dimension_and_string_helpers(tmp_path):
    assert ffmpeg_mod._normalized_rotation("449") == 90
    assert ffmpeg_mod._normalized_rotation("bad") == 0
    assert ffmpeg_mod._stream_rotation({"side_data_list": [{"rotation": -91}], "tags": {"rotate": "0"}}) == -90
    assert ffmpeg_mod._stream_rotation({"tags": {"rotate": "181"}}) == -180
    assert ffmpeg_mod._display_dimensions({"width": "1080", "height": "1920"}, 90) == (1920, 1080)
    assert ffmpeg_mod._target_resolution(1080, 1920) == (1080, 1920)
    assert ffmpeg_mod._target_resolution(1920, 1080) == (1920, 1080)
    assert ffmpeg_mod._color_value(" unknown ") is None
    assert ffmpeg_mod._color_value("BT2020") == "bt2020"
    assert ffmpeg_mod._is_hdr_color("smpte2084", None) is True
    assert ffmpeg_mod._is_hdr_color(None, "bt709") is False
    assert ffmpeg_mod._ffmpeg_filter_arg(tmp_path / "a'b\\c.trf").startswith("'")
    assert ffmpeg_mod._concat_file_line(tmp_path / "a'b.mp4").startswith("file '")
