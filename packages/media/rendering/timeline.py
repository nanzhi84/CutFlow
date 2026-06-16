"""ffmpeg render command builders shared by production nodes."""

from __future__ import annotations

import functools
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from packages.core.contracts import Artifact, ErrorCode, MediaInfo
from packages.core.workflow import NodeExecutionError
from packages.media.video.ffmpeg import (
    FfmpegRunner,
    ffmpeg_bin,
    probe_media,
    probe_video_frame_count,
)


_DEFAULT_RENDER_MAX_INFLIGHT = 2
_RENDER_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_RENDER_SEMAPHORES_LOCK = threading.Lock()


def _render_max_inflight() -> int:
    raw = os.getenv("CUTAGENT_RENDER_MAX_INFLIGHT")
    if raw is None or raw == "":
        return _DEFAULT_RENDER_MAX_INFLIGHT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_RENDER_MAX_INFLIGHT


def _render_semaphore(key: str) -> threading.BoundedSemaphore:
    with _RENDER_SEMAPHORES_LOCK:
        semaphore = _RENDER_SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(_render_max_inflight())
            _RENDER_SEMAPHORES[key] = semaphore
        return semaphore


@contextmanager
def render_slot(key: str) -> Iterator[None]:
    semaphore = _render_semaphore(key)
    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


def _limit_render_slot(key: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with render_slot(key):
                return func(*args, **kwargs)

        return wrapper

    return decorator


@dataclass(frozen=True)
class RenderExpectation:
    expected_frames: int
    expected_width: int | None = None
    expected_height: int | None = None
    expected_fps: int | None = None


def validate_rendered_output(
    output_path: Path,
    *,
    expected_frames: int,
    expected_width: int | None = None,
    expected_height: int | None = None,
    expected_fps: int | None = None,
    frame_count_message: str = "Rendered timeline frame count does not match the plan.",
    media_info_message: str = "Rendered timeline media info does not match the plan.",
) -> MediaInfo:
    expectation = RenderExpectation(
        expected_frames=expected_frames,
        expected_width=expected_width,
        expected_height=expected_height,
        expected_fps=expected_fps,
    )
    media_info = probe_media(output_path)
    frame_count = probe_video_frame_count(output_path)
    if frame_count != expectation.expected_frames:
        raise NodeExecutionError(
            ErrorCode.render_invalid_timeline,
            frame_count_message,
        )
    if (
        (expectation.expected_width is not None and media_info.width != expectation.expected_width)
        or (expectation.expected_height is not None and media_info.height != expectation.expected_height)
        or (
            expectation.expected_fps is not None
            and round(media_info.fps or 0) != expectation.expected_fps
        )
    ):
        raise NodeExecutionError(
            ErrorCode.render_invalid_timeline,
            media_info_message,
        )
    return media_info


def generate_seed_video(
    output_path: Path,
    *,
    duration_sec: float,
    width: int,
    height: int,
    fps: int,
) -> None:
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate={fps}",
            "-t",
            f"{duration_sec:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def generate_seed_audio(output_path: Path, *, duration_sec: float) -> None:
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:sample_rate=44100:duration={duration_sec:.3f}",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )


def transcode_video_segment(
    source_path: Path,
    output_path: Path,
    *,
    source_start: float,
    duration: float,
    width: int,
    height: int,
    fps: int,
) -> None:
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{source_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source_path),
            "-an",
            "-vf",
            (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={fps},setsar=1"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def concat_video_segments(segments: list[Path], output_path: Path) -> None:
    concat_list = output_path.with_suffix(".txt")
    concat_list.write_text(
        "\n".join(f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in segments),
        encoding="utf-8",
    )
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def fit_video_to_exact_duration(
    source_path: Path,
    output_path: Path,
    *,
    duration: float,
    width: int,
    height: int,
    fps: int,
) -> None:
    """Force a rendered track to be exactly ``duration`` seconds long.

    Per-segment ``-t`` ms-quantization, fps resampling and ``concat -c copy``
    accumulate sub-frame timing drift that, for longer tracks, exceeds the
    one-frame tolerance of the portrait-track sanity check. This re-encodes the
    concatenated track to a deterministic length so the check passes reliably:

    - ``tpad=stop_mode=clone`` clones the final frame to *pad* a short track
      past the target (the clone padding is generous: it always exceeds
      ``duration`` so the subsequent trim is what sets the exact length).
    - ``-t {duration:.3f}`` then *trims* to exactly ``duration``.

    The result is guaranteed ``>=`` the plan duration (no end freeze/black for a
    track that was already long enough) and never materially longer. One extra
    ffmpeg pass; the track is short, so re-encoding is cheap.
    """
    pad_duration = max(duration, 0.0) + 1.0
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_path),
            "-an",
            "-vf",
            (
                f"tpad=stop_mode=clone:stop_duration={pad_duration:.3f},"
                f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1"
            ),
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


@_limit_render_slot("render.cpu.heavy")
def render_video_timeline(
    *,
    main_path: Path,
    output_path: Path,
    broll_segments: list[dict],
    total_frames: int,
    width: int,
    height: int,
    fps: int,
    source_artifact_for_asset: Callable[[str | None], Artifact],
    artifact_path: Callable[[Artifact], Path],
) -> None:
    args = [
        ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(main_path),
    ]
    overlay_inputs: list[tuple[dict, Path]] = []
    for segment in broll_segments:
        source_artifact = source_artifact_for_asset(segment.get("asset_id"))
        source_path = artifact_path(source_artifact)
        source_info = source_artifact.media_info or probe_media(source_path)
        source_duration = float(source_info.duration_sec or 0)
        source_start = float(segment.get("source_start", 0) or 0)
        source_end = float(segment.get("source_end", 0) or 0)
        if source_start < 0 or source_end <= source_start or source_end > source_duration + (1 / fps):
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "B-roll source window is out of bounds.")
        overlay_inputs.append((segment, source_path))
        args.extend(["-i", str(source_path)])

    filters = [
        (
            f"[0:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},trim=start_frame=0:end_frame={total_frames},"
            "setpts=PTS-STARTPTS,setsar=1[base0]"
        )
    ]
    previous_label = "base0"
    total_duration = total_frames / fps
    for index, (segment, _) in enumerate(overlay_inputs, start=1):
        timeline_start = float(segment.get("start_sec", 0) or 0)
        timeline_end = float(segment.get("end_sec", 0) or 0)
        if timeline_start < 0 or timeline_end <= timeline_start or timeline_end > total_duration + (1 / fps):
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "B-roll timeline window is out of bounds.")
        source_start = float(segment.get("source_start", 0) or 0)
        source_end = float(segment.get("source_end", 0) or 0)
        overlay_label = f"ov{index}"
        next_label = f"base{index}"
        filters.append(
            (
                f"[{index}:v]trim=start={source_start:.3f}:end={source_end:.3f},"
                "setpts=PTS-STARTPTS,"
                f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,"
                f"setpts=PTS-STARTPTS+{timeline_start:.3f}/TB[{overlay_label}]"
            )
        )
        filters.append(
            (
                f"[{previous_label}][{overlay_label}]overlay="
                f"enable='between(t,{timeline_start:.3f},{timeline_end:.3f})':"
                f"x=0:y=0:eof_action=pass[{next_label}]"
            )
        )
        previous_label = next_label

    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{previous_label}]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    FfmpegRunner(timeout_sec=60).run(args)


def _escape_subtitle_filter_value(value: str) -> str:
    """Escape a path for use inside an ffmpeg ``subtitles`` filter argument."""
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
