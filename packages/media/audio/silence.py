"""Audio-pause (silence) DETECTION via ffmpeg ``silencedetect``.

Ported from digital-human-Cutagent's ``editing_agent_service._detect_audio_pause_windows``
(byte-faithful thresholds/parse/merge). This is the IO half of the editing-agent
audio-pause path: it runs ffmpeg locally on a produced TTS audio artifact and turns
``silencedetect`` log lines into pause windows. The PURE matcher
(:mod:`packages.planning.editing.audio_pause`) then snaps semantic boundaries into
these windows.

No network, no paid calls — ffmpeg subprocess only. When the audio is the sandbox
440Hz tone (no real silences) this returns ~no windows, so the boundary planner
falls back to semantic-only boundaries. Pauses are NEVER fabricated; a missing file
or an ffmpeg failure yields an empty list (honest "no pauses detected").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from packages.media.video.ffmpeg import FfmpegCommandError, FfmpegRunner, ffmpeg_bin

# Detection thresholds — mirror EditingAgentSettings.audio_pause_* defaults.
AUDIO_PAUSE_NOISE_DB = -32.0
AUDIO_PAUSE_MIN_DURATION = 0.05
# Adjacent windows closer than this gap are merged into one (origin's 0.02s).
AUDIO_PAUSE_MERGE_GAP = 0.02
SILENCEDETECT_TIMEOUT_SEC = 90

_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")

# Process-local cache keyed by normalized path + thresholds + a content signature
# (mtime, size): detection is deterministic for a given file, so re-running ffmpeg on
# the same unchanged artifact within a run is wasteful; the signature ensures an
# overwritten/regenerated file at the same path is re-detected, not served stale.
_DETECTION_CACHE: Dict[str, List[Dict[str, float]]] = {}


def _round_time(value: float) -> float:
    return round(max(0.0, float(value)), 3)


def detect_silence_windows(
    audio_path: str | Path,
    *,
    noise_db: float = AUDIO_PAUSE_NOISE_DB,
    min_duration: float = AUDIO_PAUSE_MIN_DURATION,
) -> List[Dict[str, float]]:
    """Detect pause windows in ``audio_path`` via ffmpeg ``silencedetect``.

    Returns merged, time-rounded windows ``{start, end, duration, center}`` (seconds).
    An empty list means no reliable silence was found (or the file is missing / ffmpeg
    failed) — the planner then uses semantic-only boundaries.
    """
    normalized = str(audio_path or "").strip()
    if not normalized:
        return []
    try:
        stat = Path(normalized).stat()
    except OSError:
        # Missing/unreadable file -> honest "no pauses". Not cached, so a later
        # write at this path re-detects.
        return []
    # Key on a content signature (mtime + size), not just the path, so an
    # overwritten/regenerated file at the same path never serves stale windows.
    cache_key = f"{normalized}|{noise_db}|{min_duration}|{stat.st_mtime_ns}:{stat.st_size}"
    cached = _DETECTION_CACHE.get(cache_key)
    if cached is not None:
        return [dict(window) for window in cached]

    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostats",
        "-i",
        normalized,
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = FfmpegRunner(timeout_sec=SILENCEDETECT_TIMEOUT_SEC).run(
            cmd, timeout_sec=SILENCEDETECT_TIMEOUT_SEC
        )
        output = "\n".join([result.stdout or "", result.stderr or ""])
    except FfmpegCommandError as exc:
        # Honest "no pauses" on failure — never fabricate. stderr still carries the
        # silencedetect lines even on a non-zero exit, so parse what we have.
        output = "\n".join([getattr(exc, "stdout", "") or "", exc.stderr or ""])
        if not output.strip():
            _DETECTION_CACHE[cache_key] = []
            return []

    windows = _parse_silence_windows(output)
    merged = _merge_adjacent_windows(windows)
    _DETECTION_CACHE[cache_key] = [dict(window) for window in merged]
    return merged


def _parse_silence_windows(output: str) -> List[Dict[str, float]]:
    current_start: float | None = None
    windows: List[Dict[str, float]] = []
    for line in output.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            current_start = max(0.0, float(start_match.group(1)))
            continue
        end_match = _SILENCE_END_RE.search(line)
        if not end_match:
            continue
        end = max(0.0, float(end_match.group(1)))
        duration = max(0.0, float(end_match.group(2)))
        start = current_start if current_start is not None else max(0.0, end - duration)
        if end > start:
            windows.append(
                {
                    "start": _round_time(start),
                    "end": _round_time(end),
                    "duration": _round_time(end - start),
                    "center": _round_time(start + ((end - start) / 2.0)),
                }
            )
        current_start = None
    return windows


def _merge_adjacent_windows(windows: List[Dict[str, float]]) -> List[Dict[str, float]]:
    merged: List[Dict[str, float]] = []
    for window in windows:
        if merged and window["start"] <= merged[-1]["end"] + AUDIO_PAUSE_MERGE_GAP:
            merged[-1]["end"] = _round_time(max(merged[-1]["end"], window["end"]))
            merged[-1]["duration"] = _round_time(merged[-1]["end"] - merged[-1]["start"])
            merged[-1]["center"] = _round_time(merged[-1]["start"] + (merged[-1]["duration"] / 2.0))
            continue
        merged.append(dict(window))
    return merged
