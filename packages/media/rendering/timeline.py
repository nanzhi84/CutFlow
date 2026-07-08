"""ffmpeg render command builders shared by production nodes."""

from __future__ import annotations

import functools
import math
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from packages.core.contracts import Artifact, ErrorCode, MediaInfo
from packages.core.workflow import NodeExecutionError
from packages.media.video.ffmpeg import (
    FfmpegRunner,
    ffmpeg_base_args,
    ffmpeg_output_thread_args,
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


def _to_frame(seconds: float, fps: int) -> int:
    return max(0, int(math.floor(float(seconds) * fps + 0.5)))


def _format_filter_seconds(seconds: float) -> str:
    text = f"{max(0.0, float(seconds)):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _ffmpeg_render_prefix() -> list[str]:
    return ffmpeg_base_args(quiet_args=("-y", "-hide_banner", "-loglevel", "error"))


def _overlay_fade_frames(segment: dict) -> int:
    value = segment.get("fade_frames")
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _overlay_placement(segment: dict) -> str:
    placement = str(segment.get("placement") or "fullscreen")
    return placement if placement in {"fullscreen", "pip_fixed"} else "fullscreen"


def _pip_geometry(width: int, height: int) -> tuple[int, int, int, int]:
    pip_width = max(2, int(width * 0.42) // 2 * 2)
    pip_height = max(2, int(height * 0.24) // 2 * 2)
    margin_x = max(16, int(width * 0.045))
    margin_y = max(16, int(height * 0.10))
    return pip_width, pip_height, width - pip_width - margin_x, margin_y


def _overlay_scale_filter(segment: dict, *, width: int, height: int) -> tuple[str, int, int]:
    if _overlay_placement(segment) == "pip_fixed":
        pip_width, pip_height, x, y = _pip_geometry(width, height)
        return (
            f"scale={pip_width}:{pip_height}:force_original_aspect_ratio=increase,"
            f"crop={pip_width}:{pip_height},setsar=1,",
            x,
            y,
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,",
        0,
        0,
    )


def _overlay_alpha_filters(fade_frames: int, timeline_window_frames: int) -> str:
    if fade_frames <= 0 or timeline_window_frames <= 1:
        return ""
    frames = min(fade_frames, max(1, timeline_window_frames // 2))
    out_start = max(0, timeline_window_frames - frames)
    return (
        "format=yuva420p,"
        f"fade=t=in:s=0:n={frames}:alpha=1,"
        f"fade=t=out:s={out_start}:n={frames}:alpha=1,"
    )


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
    media_info = probe_media(output_path)
    frame_count = probe_video_frame_count(output_path)
    if frame_count != expected_frames:
        raise NodeExecutionError(
            ErrorCode.render_invalid_timeline,
            frame_count_message,
            details={"expected_frames": expected_frames, "actual_frames": frame_count},
        )
    if (
        (expected_width is not None and media_info.width != expected_width)
        or (expected_height is not None and media_info.height != expected_height)
        or (expected_fps is not None and round(media_info.fps or 0) != expected_fps)
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
            *_ffmpeg_render_prefix(),
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate={fps}",
            "-t",
            f"{duration_sec:.3f}",
            "-pix_fmt",
            "yuv420p",
            *ffmpeg_output_thread_args(),
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
            *_ffmpeg_render_prefix(),
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
    source_start_frame: int,
    source_end_frame: int,
    width: int,
    height: int,
    fps: int,
) -> None:
    FfmpegRunner().run(
        [
            *_ffmpeg_render_prefix(),
            "-i",
            str(source_path),
            "-an",
            "-vf",
            (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={fps},"
                f"trim=start_frame={source_start_frame}:end_frame={source_end_frame},"
                "setpts=PTS-STARTPTS,setsar=1"
            ),
            *ffmpeg_output_thread_args(),
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
            *_ffmpeg_render_prefix(),
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
            *_ffmpeg_render_prefix(),
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
            *ffmpeg_output_thread_args(),
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
def render_broll_montage(
    *,
    segments: list[dict],
    output_path: Path,
    total_frames: int,
    width: int,
    height: int,
    fps: int,
    source_artifact_for_asset: Callable[[str], object],
    artifact_path: Callable[[object], Path],
) -> None:
    """Concatenate ordered b-roll windows into an exact-frame silent base video."""
    if total_frames <= 0 or fps <= 0 or not segments:
        raise NodeExecutionError(
            ErrorCode.render_invalid_timeline,
            "B-roll montage timeline is invalid.",
        )

    args = [
        *_ffmpeg_render_prefix(),
    ]
    montage_inputs: list[tuple[int, int]] = []
    for segment in segments:
        source_artifact = source_artifact_for_asset(str(segment.get("asset_id") or ""))
        source_path = artifact_path(source_artifact)
        source_info = getattr(source_artifact, "media_info", None) or probe_media(source_path)
        source_duration = float(source_info.duration_sec or 0)
        source_start = float(segment.get("source_start", 0) or 0)
        source_end = float(segment.get("source_end", 0) or 0)
        timeline_start = float(segment.get("timeline_start", segment.get("start_sec", 0)) or 0)
        timeline_end = float(segment.get("timeline_end", segment.get("end_sec", 0)) or 0)
        source_start_frame = (
            int(segment["source_start_frame"])
            if segment.get("source_start_frame") is not None
            else _to_frame(source_start, fps)
        )
        source_end_frame = (
            int(segment["source_end_frame"])
            if segment.get("source_end_frame") is not None
            else _to_frame(source_end, fps)
        )
        timeline_start_frame = (
            int(segment["timeline_start_frame"])
            if segment.get("timeline_start_frame") is not None
            else _to_frame(timeline_start, fps)
        )
        timeline_end_frame = (
            int(segment["timeline_end_frame"])
            if segment.get("timeline_end_frame") is not None
            else _to_frame(timeline_end, fps)
        )
        source_duration_frames = _to_frame(source_duration, fps)
        timeline_window_frames = timeline_end_frame - timeline_start_frame
        source_window_frames = source_end_frame - source_start_frame
        window_frames = min(source_window_frames, timeline_window_frames)
        if (
            source_start_frame < 0
            or source_end_frame <= source_start_frame
            or source_end_frame > source_duration_frames
        ):
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                "B-roll source window is out of bounds.",
            )
        if (
            timeline_start_frame < 0
            or timeline_end_frame <= timeline_start_frame
            or timeline_end_frame > total_frames
        ):
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                "B-roll timeline window is out of bounds.",
            )
        montage_inputs.append((source_start_frame, window_frames))
        args.extend(["-i", str(source_path)])

    filters = []
    for index, (source_start_frame, window_frames) in enumerate(montage_inputs):
        filters.append(
            (
                f"[{index}:v]fps={fps},"
                f"trim=start_frame={source_start_frame}:end_frame={source_start_frame + window_frames},"
                "setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[seg{index}]"
            )
        )
    concat_inputs = "".join(f"[seg{index}]" for index in range(len(montage_inputs)))
    filters.append(
        (
            f"{concat_inputs}concat=n={len(montage_inputs)}:v=1:a=0,"
            # Per-segment fps resampling floors sub-frame tails, so the raw concat can
            # land a frame short of total_frames (real narration durations are rarely
            # frame-aligned). Clone the final frame to overshoot the target, then the
            # frame-exact trim below sets the precise length -- mirrors
            # fit_video_to_exact_duration on the A-roll base track. Without this, a
            # short montage trips validate_rendered_output's exact-frame check and the
            # whole render hard-fails with a misleading render_invalid_timeline.
            f"tpad=stop_mode=clone:stop={fps},"
            f"trim=start_frame=0:end_frame={total_frames},setpts=PTS-STARTPTS[outv]"
        )
    )
    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-an",
            *ffmpeg_output_thread_args(),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-frames:v",
            str(total_frames),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    FfmpegRunner(timeout_sec=60).run(args)


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
        *_ffmpeg_render_prefix(),
        "-i",
        str(main_path),
    ]
    overlay_inputs: list[tuple[dict, Path, int, int, int, int, float, float, int]] = []
    for segment in broll_segments:
        source_artifact = source_artifact_for_asset(segment.get("asset_id"))
        source_path = artifact_path(source_artifact)
        source_info = source_artifact.media_info or probe_media(source_path)
        source_duration = float(source_info.duration_sec or 0)
        source_start = float(segment.get("source_start", 0) or 0)
        source_end = float(segment.get("source_end", 0) or 0)
        timeline_start = float(segment.get("start_sec", segment.get("timeline_start", 0)) or 0)
        timeline_end = float(segment.get("end_sec", segment.get("timeline_end", 0)) or 0)
        source_start_frame = (
            int(segment["source_start_frame"])
            if segment.get("source_start_frame") is not None
            else _to_frame(source_start, fps)
        )
        source_end_frame = (
            int(segment["source_end_frame"])
            if segment.get("source_end_frame") is not None
            else _to_frame(source_end, fps)
        )
        timeline_start_frame = (
            int(segment["timeline_start_frame"])
            if segment.get("timeline_start_frame") is not None
            else _to_frame(timeline_start, fps)
        )
        timeline_end_frame = (
            int(segment["timeline_end_frame"])
            if segment.get("timeline_end_frame") is not None
            else _to_frame(timeline_end, fps)
        )
        pad_start = max(0.0, float(segment.get("pad_start", 0) or 0))
        pad_end = max(0.0, float(segment.get("pad_end", 0) or 0))
        source_duration_frames = _to_frame(source_duration, fps)
        if source_start_frame < 0 or source_end_frame <= source_start_frame or source_end_frame > source_duration_frames:
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "B-roll source window is out of bounds.")
        if not (0 <= timeline_start_frame < timeline_end_frame <= total_frames):
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "B-roll timeline window is out of bounds.")
        overlay_inputs.append(
            (
                segment,
                source_path,
                source_start_frame,
                source_end_frame,
                timeline_start_frame,
                timeline_end_frame,
                pad_start,
                pad_end,
                _overlay_fade_frames(segment),
            )
        )
        args.extend(["-i", str(source_path)])

    filters = [
        (
            f"[0:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"tpad=stop_mode=clone:stop={total_frames},"
            f"trim=start_frame=0:end_frame={total_frames},"
            "setpts=PTS-STARTPTS,setsar=1[base0]"
        )
    ]
    previous_label = "base0"
    for index, (
        segment,
        _,
        source_start_frame,
        source_end_frame,
        timeline_start_frame,
        timeline_end_frame,
        pad_start,
        pad_end,
        fade_frames,
    ) in enumerate(overlay_inputs, start=1):
        timeline_window_frames = timeline_end_frame - timeline_start_frame
        overlay_label = f"ov{index}"
        next_label = f"base{index}"
        scale_filter, overlay_x, overlay_y = _overlay_scale_filter(
            segment, width=width, height=height
        )
        explicit_padding = ""
        if pad_start > 0:
            explicit_padding += f"tpad=start_duration={_format_filter_seconds(pad_start)}:start_mode=clone,"
        if pad_end > 0:
            explicit_padding += f"tpad=stop_duration={_format_filter_seconds(pad_end)}:stop_mode=clone,"
        filters.append(
            (
                f"[{index}:v]fps={fps},"
                f"trim=start_frame={source_start_frame}:end_frame={source_end_frame},"
                "setpts=PTS-STARTPTS,"
                f"{scale_filter}"
                f"{explicit_padding}"
                f"tpad=stop_mode=clone:stop={timeline_window_frames},"
                f"trim=start_frame=0:end_frame={timeline_window_frames},"
                f"{_overlay_alpha_filters(fade_frames, timeline_window_frames)}"
                f"setpts=PTS-STARTPTS+{timeline_start_frame}/{fps}/TB[{overlay_label}]"
            )
        )
        filters.append(
            (
                f"[{previous_label}][{overlay_label}]overlay="
                f"enable='gte(n\\,{timeline_start_frame})*lt(n\\,{timeline_end_frame})':"
                f"x={overlay_x}:y={overlay_y}:eof_action=pass[{next_label}]"
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
            *ffmpeg_output_thread_args(),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-frames:v",
            str(total_frames),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    FfmpegRunner(timeout_sec=60).run(args)


def _escape_subtitle_filter_value(value: str) -> str:
    """Escape a path for use inside an ffmpeg ``subtitles`` filter argument."""
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
