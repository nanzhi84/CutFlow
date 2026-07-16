from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from packages.media.video.ffmpeg import FfmpegCommandError, FfmpegRunner, ffmpeg_bin

logger = logging.getLogger("packages.media.audio.loudness")


def _extract_loudnorm_json(output: str) -> dict | None:
    text = output or ""
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def measure_loudness_lufs(media_path: str | Path) -> float | None:
    """Measure integrated loudness (LUFS) via ffmpeg loudnorm analysis."""
    path = Path(media_path)
    if not path.exists():
        return None
    args = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vn",
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        result = FfmpegRunner(timeout_sec=120).run(args)
    except (FfmpegCommandError, OSError) as exc:
        logger.warning("[bgm] loudness probe failed for %s: %s", path, exc)
        return None
    data = _extract_loudnorm_json(f"{result.stdout or ''}\n{result.stderr or ''}")
    if not data:
        return None
    try:
        loudness = float(data.get("input_i"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(loudness) or loudness <= -99:
        return None
    return loudness
