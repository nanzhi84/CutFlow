"""Picture-quality sensor: black / freeze / blur (beyond motion_guard's shake/drop).

- black:  ffmpeg ``blackdetect``, parse black_start/black_end from stderr;
- freeze: ffmpeg ``freezedetect``, parse freeze_start/freeze_end from stderr;
- blur:   sample frames at ``blur_sample_fps``, compute Laplacian variance per
          frame, merge contiguous low-variance frames into blur segments.

Sensor discipline: the parsing/merge logic is pure (``parse_blackdetect`` /
``parse_freezedetect`` / ``merge_blur_segments``), unit-testable on synthetic
text/number sequences. Fail-open: ffmpeg/cv2 unavailable or a video that won't
open returns [] (no negative evidence), never raises, never fabricates.

Event-type mapping (reuses QualityEventType): black / freeze -> OCCLUSION (hard);
blur -> BLUR (soft). Returned event dicts align QualityEventV4 fields (event_id
added later by the assembler).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Sequence

from packages.core.contracts import QualityEventType

from .._util import TIME_DECIMALS as _TIME_DECIMALS

logger = logging.getLogger(__name__)

# blackdetect default pixel threshold (how "black" counts); lower is stricter.
_DEFAULT_BLACK_PIX_TH = 0.10
# freezedetect default noise tolerance (dB); higher is more lenient.
_DEFAULT_FREEZE_NOISE_DB = -60.0
# Laplacian-variance threshold; strictly below this is a blur frame
# (320-long-side grayscale empirical: sharp >100, blurry <60).
_DEFAULT_BLUR_VARIANCE_THRESHOLD = 60.0
# Grayscale long side for blur sampling (control compute, like motion_guard).
_BLUR_FRAME_LONG_SIDE = 320
_FFMPEG_FILTER_TIMEOUT_SEC = 180

# ffmpeg stderr regexes: optional space after colon, float value.
_BLACK_START_RE = re.compile(r"black_start\s*:\s*([0-9]+(?:\.[0-9]+)?)")
_BLACK_END_RE = re.compile(r"black_end\s*:\s*([0-9]+(?:\.[0-9]+)?)")
_FREEZE_START_RE = re.compile(r"freeze_start\s*:\s*([0-9]+(?:\.[0-9]+)?)")
_FREEZE_END_RE = re.compile(r"freeze_end\s*:\s*([0-9]+(?:\.[0-9]+)?)")


# ===========================================================================
# Pure functions: ffmpeg stderr -> event intervals (testable on synthetic text)
# ===========================================================================
def parse_blackdetect(
    stderr_text: str, *, total_duration: float = 0.0
) -> list[tuple[float, float]]:
    """Parse blackdetect stderr into ascending black intervals ``[(start, end), ...]``.

    Each detected black span prints one line like
    ``[blackdetect @ ..] black_start:1 black_end:1.96 black_duration:0.96``.
    A line with both start and end is a complete interval; a start without an end
    (black runs to EOF) closes at ``total_duration`` (dropped if <=0). Zero-length
    / reversed intervals are dropped.
    """
    intervals: list[tuple[float, float]] = []
    for line in (stderr_text or "").splitlines():
        start_match = _BLACK_START_RE.search(line)
        if not start_match:
            continue
        start = float(start_match.group(1))
        end_match = _BLACK_END_RE.search(line)
        if end_match:
            end = float(end_match.group(1))
        elif total_duration and total_duration > start:
            end = float(total_duration)
        else:
            continue
        if end > start:
            intervals.append(
                (round(start, _TIME_DECIMALS), round(end, _TIME_DECIMALS))
            )
    intervals.sort(key=lambda pair: pair[0])
    return intervals


def parse_freezedetect(
    stderr_text: str, *, total_duration: float = 0.0
) -> list[tuple[float, float]]:
    """Parse freezedetect stderr into ascending freeze intervals.

    freezedetect prints start / duration / end on separate lines. State machine:
    a freeze_start opens a span, the following freeze_end closes it; a final
    open start closes at ``total_duration`` (dropped if no basis). Zero-length /
    reversed intervals are dropped.
    """
    intervals: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in (stderr_text or "").splitlines():
        start_match = _FREEZE_START_RE.search(line)
        if start_match:
            if (
                pending_start is not None
                and total_duration
                and total_duration > pending_start
            ):
                intervals.append(
                    (pending_start, round(float(total_duration), _TIME_DECIMALS))
                )
            pending_start = round(float(start_match.group(1)), _TIME_DECIMALS)
            continue
        end_match = _FREEZE_END_RE.search(line)
        if end_match and pending_start is not None:
            end = round(float(end_match.group(1)), _TIME_DECIMALS)
            if end > pending_start:
                intervals.append((pending_start, end))
            pending_start = None

    if pending_start is not None and total_duration and total_duration > pending_start:
        intervals.append((pending_start, round(float(total_duration), _TIME_DECIMALS)))

    intervals.sort(key=lambda pair: pair[0])
    return intervals


# ===========================================================================
# Pure function: per-frame Laplacian-variance sequence -> blur segments
# ===========================================================================
def merge_blur_segments(
    times: Sequence[float],
    variances: Sequence[float],
    *,
    threshold: float,
    min_dur: float,
) -> list[tuple[float, float]]:
    """Merge a per-frame Laplacian-variance sequence into blur segments.

    A frame with variance strictly below ``threshold`` is a blur frame (equal is
    not, to avoid boundary false positives). Contiguous blur frames merge into a
    segment: start = first frame time; end = last frame time + one step (so a
    single-frame segment isn't zero-length); the step is the median frame
    interval (robust to non-uniform sampling), degrading to the last frame time
    when unknowable. Segments shorter than ``min_dur`` are dropped. ``times`` and
    ``variances`` must be equal length and ascending; a length mismatch returns [].
    """
    n = len(times)
    if n == 0 or n != len(variances):
        return []

    step = _median_step(times)

    segments: list[tuple[float, float]] = []
    run_start_idx: int | None = None
    for idx in range(n):
        is_blur = float(variances[idx]) < float(threshold)
        if is_blur:
            if run_start_idx is None:
                run_start_idx = idx
        else:
            if run_start_idx is not None:
                segments.append(_close_blur_run(times, run_start_idx, idx - 1, step))
                run_start_idx = None
    if run_start_idx is not None:
        segments.append(_close_blur_run(times, run_start_idx, n - 1, step))

    return [(s, e) for (s, e) in segments if (e - s) >= float(min_dur)]


def _median_step(times: Sequence[float]) -> float:
    """Median of adjacent time differences; 0 for fewer than two frames."""
    diffs = [
        float(times[i + 1]) - float(times[i])
        for i in range(len(times) - 1)
        if float(times[i + 1]) - float(times[i]) > 0
    ]
    if not diffs:
        return 0.0
    diffs.sort()
    mid = len(diffs) // 2
    if len(diffs) % 2:
        return diffs[mid]
    return (diffs[mid - 1] + diffs[mid]) / 2.0


def _close_blur_run(
    times: Sequence[float], left: int, right: int, step: float
) -> tuple[float, float]:
    """Close a contiguous blur run ``[left, right]`` into (start, end); tail gets one step."""
    start = float(times[left])
    end = float(times[right]) + (step if step > 0 else 0.0)
    return (round(start, _TIME_DECIMALS), round(end, _TIME_DECIMALS))


# ===========================================================================
# Event builders (align QualityEventV4, no event_id - added by the assembler)
# ===========================================================================
def _build_black_event(start: float, end: float) -> dict:
    return {
        "event_type": QualityEventType.occlusion.value,
        "start": round(float(start), _TIME_DECIMALS),
        "end": round(float(end), _TIME_DECIMALS),
        "risk_tier": "hard",
        "confidence": 0.95,
        "severity": 0.9,
        "source": "sensor",
        "description": f"sensor(black): pure black {start:.2f}~{end:.2f}s, unusable.",
    }


def _build_freeze_event(start: float, end: float) -> dict:
    return {
        "event_type": QualityEventType.occlusion.value,
        "start": round(float(start), _TIME_DECIMALS),
        "end": round(float(end), _TIME_DECIMALS),
        "risk_tier": "hard",
        "confidence": 0.9,
        "severity": 0.85,
        "source": "sensor",
        "description": f"sensor(freeze): frozen/stuck frame {start:.2f}~{end:.2f}s, unusable.",
    }


def _build_blur_event(start: float, end: float) -> dict:
    return {
        "event_type": QualityEventType.blur.value,
        "start": round(float(start), _TIME_DECIMALS),
        "end": round(float(end), _TIME_DECIMALS),
        "risk_tier": "soft",
        "confidence": 0.7,
        "severity": 0.5,
        "source": "sensor",
        "description": f"sensor(blur): blurry/out-of-focus {start:.2f}~{end:.2f}s.",
    }


# ===========================================================================
# Public interface
# ===========================================================================
def detect_cv_quality_events(
    video_path: str,
    *,
    blur_sample_fps: float = 2.0,
    black_min_dur: float = 0.3,
    freeze_min_dur: float = 0.5,
) -> list[dict]:
    """Detect deterministic picture-quality events: black / freeze / blur.

    Returns event dicts aligned with QualityEventV4 (no event_id), ascending by
    start. ffmpeg/cv2 unavailable or a video that won't open returns [].
    """
    if not video_path or not os.path.exists(video_path):
        logger.warning("[cv_quality] video not found, returning empty: %s", video_path)
        return []

    total_duration = _probe_duration(video_path)
    events: list[dict] = []

    black_stderr = _run_ffmpeg_filter(
        video_path,
        f"blackdetect=d={float(black_min_dur):.3f}:pix_th={_DEFAULT_BLACK_PIX_TH}",
    )
    if black_stderr is not None:
        for start, end in parse_blackdetect(black_stderr, total_duration=total_duration):
            events.append(_build_black_event(start, end))

    freeze_stderr = _run_ffmpeg_filter(
        video_path,
        f"freezedetect=d={float(freeze_min_dur):.3f}:noise={_DEFAULT_FREEZE_NOISE_DB}dB",
    )
    if freeze_stderr is not None:
        for start, end in parse_freezedetect(
            freeze_stderr, total_duration=total_duration
        ):
            events.append(_build_freeze_event(start, end))

    for start, end in _detect_blur_segments(video_path, blur_sample_fps=blur_sample_fps):
        events.append(_build_blur_event(start, end))

    events.sort(key=lambda e: e["start"])
    return events


def _probe_duration(video_path: str) -> float:
    """Estimate duration via cv2 (frames / fps); 0 when unavailable."""
    try:
        import cv2  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency missing
        logger.debug("[cv_quality] cv2 unavailable, cannot probe duration: %s", exc)
        return 0.0
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        return 0.0
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
    finally:
        cap.release()
    if fps > 0 and frame_count > 0:
        return round(frame_count / fps, _TIME_DECIMALS)
    return 0.0


def _run_ffmpeg_filter(video_path: str, vf: str) -> str | None:
    """Run one ffmpeg filter (stderr only, video output discarded to null).

    Returns stderr text; ffmpeg unavailable / call failure returns None.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        video_path,
        "-vf",
        vf,
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=_FFMPEG_FILTER_TIMEOUT_SEC
        )
    except Exception as exc:  # pragma: no cover - depends on local ffmpeg
        logger.warning("[cv_quality] ffmpeg filter failed (%s): %s", vf, exc)
        return None
    return (result.stderr or b"").decode("utf-8", "ignore")


def _detect_blur_segments(
    video_path: str,
    *,
    blur_sample_fps: float,
) -> list[tuple[float, float]]:
    """Sample frames at ``blur_sample_fps``, compute Laplacian variance, merge segments.

    cv2 unavailable / won't open / illegal fps returns [].
    """
    try:
        import cv2  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency missing
        logger.debug("[cv_quality] cv2 unavailable, skipping blur: %s", exc)
        return []

    sample_fps = float(blur_sample_fps)
    if sample_fps <= 0:
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        return []

    try:
        src_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if src_fps <= 0 or frame_count <= 0:
            return []

        step = max(1, int(round(src_fps / sample_fps)))
        times: list[float] = []
        variances: list[float] = []
        for frame_idx in range(0, frame_count, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = _to_small_gray(cv2, frame)
            variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            times.append(round(frame_idx / src_fps, _TIME_DECIMALS))
            variances.append(variance)
    except Exception as exc:  # pragma: no cover - decode failure
        logger.debug("[cv_quality] blur sampling failed %s: %s", video_path, exc)
        return []
    finally:
        cap.release()

    if not times:
        return []

    min_dur = 2.0 / sample_fps
    return merge_blur_segments(
        times,
        variances,
        threshold=_DEFAULT_BLUR_VARIANCE_THRESHOLD,
        min_dur=min_dur,
    )


def _to_small_gray(cv2_module, frame):
    """Downscale a BGR frame to ~``_BLUR_FRAME_LONG_SIDE`` long side and grayscale."""
    height, width = frame.shape[:2]
    long_side = max(height, width)
    if long_side > _BLUR_FRAME_LONG_SIDE and long_side > 0:
        scale = _BLUR_FRAME_LONG_SIDE / float(long_side)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
        frame = cv2_module.resize(
            frame, (new_w, new_h), interpolation=cv2_module.INTER_AREA
        )
    return cv2_module.cvtColor(frame, cv2_module.COLOR_BGR2GRAY)
