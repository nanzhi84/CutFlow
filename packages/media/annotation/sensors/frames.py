"""Frame extraction sensor.

Pull single frames at given timestamps via ffmpeg, downscaling the long side to
a bound so downstream consumers (VLM input, later step) stay within budget.
Deterministic and key-free; the scale filter keeps aspect ratio, makes both
dimensions even (yuv420p safe), and only shrinks (never upscales).
"""

from __future__ import annotations

import logging
import os
import uuid

from packages.media.video.ffmpeg import FfmpegCommandError, FfmpegRunner, ffmpeg_bin

logger = logging.getLogger(__name__)

DEFAULT_MAX_LONG_SIDE = 1024
EXTRACT_TIMEOUT_SEC = 30


def _build_downscale_filter(max_long_side: int) -> str:
    """Build an ffmpeg scale filter capping the long side at ``max_long_side``.

    Keeps aspect ratio, only shrinks (min(side, cap)), forces even dimensions
    (the other side uses -2 for an even multiple), and auto-detects landscape vs
    portrait via gt(iw,ih).
    """
    long = int(max_long_side)
    w_expr = f"if(gt(iw,ih),min(iw,{long}),-2)"
    h_expr = f"if(gt(iw,ih),-2,min(ih,{long}))"
    return f"scale='{w_expr}':'{h_expr}'"


def extract_frame_at_time(
    video_path: str,
    time_sec: float,
    output_path: str,
    *,
    max_long_side: int = DEFAULT_MAX_LONG_SIDE,
) -> bool:
    """Extract the frame at ``time_sec`` to ``output_path``.

    ffmpeg does not create the output's parent directory; create it first so a
    missing dir does not silently produce an empty frame. Returns True on
    success.
    """
    try:
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-ss",
            str(time_sec),
            "-i",
            video_path,
            "-vframes",
            "1",
            "-vf",
            _build_downscale_filter(max_long_side),
            "-q:v",
            "2",
            output_path,
        ]
        FfmpegRunner(timeout_sec=EXTRACT_TIMEOUT_SEC).run(cmd)
        return os.path.exists(output_path)
    except (FfmpegCommandError, OSError) as exc:  # pragma: no cover - local ffmpeg
        logger.warning("[frames] extract failed at %ss: %s", time_sec, exc)
        return False


def extract_frames_for_times(
    video_path: str,
    sample_times: list[float],
    *,
    temp_dir: str,
    max_long_side: int = DEFAULT_MAX_LONG_SIDE,
) -> list[tuple[float, str]]:
    """Extract frames at each timestamp; return ``[(time, frame_path), ...]``.

    Frames that fail extraction are skipped, so the result may be shorter than
    ``sample_times``.
    """
    frames: list[tuple[float, str]] = []
    os.makedirs(temp_dir, exist_ok=True)
    for idx, point in enumerate(sample_times):
        frame_path = os.path.join(temp_dir, f"frame_{uuid.uuid4().hex[:8]}_{idx}.jpg")
        if extract_frame_at_time(
            video_path, point, frame_path, max_long_side=max_long_side
        ):
            frames.append((round(point, 3), frame_path))
    return frames
