"""Deterministic motion-guard sensor for camera shake and camera-drop events.

The numeric core is dependency-free and unit-testable on synthetic frame-pair
motion. The IO shell lazily imports cv2/numpy and decodes a 360px-wide grayscale
stream with ffmpeg; missing dependencies, unreadable video, or decode failures
return [] rather than raising.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from packages.core.config import build_settings
from packages.core.config.settings import MotionGuardSettings
from packages.core.contracts import QualityEventType
from packages.media.video.ffmpeg import ffmpeg_bin

from .._util import TIME_DECIMALS as _TIME_DECIMALS

logger = logging.getLogger(__name__)

_MIN_WINDOW_SEC = 0.8
_MIN_WINDOW_PAIRS = 8
_MERGE_GAP_SEC = 0.15


def summarize_window(
    pairs: Sequence[tuple[float, float, float] | Mapping[str, Any]],
    *,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate adjacent-frame motion estimates into deterministic window metrics."""
    resolved = _resolve_thresholds(thresholds)
    active_px = float(resolved["active_px"])
    hard_px = float(resolved["hard_px"])
    sample_fps = max(1.0, float(resolved["sample_fps"]))

    dxs: list[float] = []
    dys: list[float] = []
    rotations: list[float] = []
    for pair in pairs:
        dx, dy, rot = _coerce_pair(pair)
        dxs.append(dx)
        dys.append(dy)
        rotations.append(abs(rot))

    count = len(dxs)
    if count == 0:
        return {
            "pairs": 0,
            "duration_sec": 0.0,
            "mag_p50": 0.0,
            "mag_p90": 0.0,
            "mag_p95": 0.0,
            "max": 0.0,
            "active_ratio": 0.0,
            "hard_ratio": 0.0,
            "max_active_run": 0,
            "cum_x_range": 0.0,
            "cum_y_range": 0.0,
            "net_x": 0.0,
            "net_y": 0.0,
            "straightness_ratio": 0.0,
            "direction_flip_ratio": 0.0,
            "jerk_p90": 0.0,
            "residual_to_p95_ratio": 0.0,
            "rot_p95_deg": 0.0,
            "path_length": 0.0,
        }

    mags = [math.hypot(dx, dy) for dx, dy in zip(dxs, dys, strict=True)]
    active = [mag > active_px for mag in mags]
    hard = [mag > hard_px for mag in mags]
    max_active_run = _max_true_run(active)
    cumulative_x = _cumsum(dxs)
    cumulative_y = _cumsum(dys)
    p95 = _percentile(mags, 95)
    path_length = sum(mags)
    net_x = cumulative_x[-1]
    net_y = cumulative_y[-1]
    net_motion = math.hypot(net_x, net_y)
    straightness = net_motion / path_length if path_length > 0 else 0.0
    x_flips, x_steps = _direction_flip_stats(dxs)
    y_flips, y_steps = _direction_flip_stats(dys)
    direction_flip_ratio = float(x_flips + y_flips) / float(max(1, x_steps + y_steps))
    jerk = [
        math.hypot(dxs[idx] - dxs[idx - 1], dys[idx] - dys[idx - 1])
        for idx in range(1, count)
    ] or [0.0]
    median_dx = _median(dxs)
    median_dy = _median(dys)
    residual = [math.hypot(dx - median_dx, dy - median_dy) for dx, dy in zip(dxs, dys)]
    residual_p90 = _percentile(residual, 90)

    return {
        "pairs": count,
        "duration_sec": round(count / sample_fps, _TIME_DECIMALS),
        "mag_p50": round(_percentile(mags, 50), 3),
        "mag_p90": round(_percentile(mags, 90), 3),
        "mag_p95": round(p95, 3),
        "max": round(max(mags), 3),
        "active_ratio": round(sum(active) / count, 3),
        "hard_ratio": round(sum(hard) / count, 3),
        "max_active_run": max_active_run,
        "cum_x_range": round(max(cumulative_x) - min(cumulative_x), 3),
        "cum_y_range": round(max(cumulative_y) - min(cumulative_y), 3),
        "net_x": round(net_x, 3),
        "net_y": round(net_y, 3),
        "straightness_ratio": round(straightness, 3),
        "direction_flip_ratio": round(direction_flip_ratio, 3),
        "jerk_p90": round(_percentile(jerk, 90), 3),
        "residual_to_p95_ratio": round(residual_p90 / (p95 + 1e-6), 3),
        "rot_p95_deg": round(_percentile(rotations, 95), 4),
        "path_length": round(path_length, 3),
    }


def classify_window(
    metrics: Mapping[str, Any],
    *,
    thresholds: Mapping[str, Any] | None = None,
    is_head: bool,
    is_tail: bool,
) -> dict[str, Any] | None:
    """Classify aggregated motion metrics as shake/camera_drop, or return None."""
    resolved = _resolve_thresholds(thresholds)
    pairs = int(_as_float(metrics.get("pairs"), 0.0))
    duration = _as_float(
        metrics.get("duration_sec"),
        pairs / max(1.0, float(resolved["sample_fps"])),
    )
    if duration < _MIN_WINDOW_SEC or pairs < _MIN_WINDOW_PAIRS:
        return None

    p95 = _as_float(metrics.get("mag_p95"), 0.0)
    active_ratio = _as_float(metrics.get("active_ratio"), 0.0)
    hard_ratio = _as_float(metrics.get("hard_ratio"), 0.0)
    max_active_run = int(_as_float(metrics.get("max_active_run"), 0.0))
    x_range = abs(_as_float(metrics.get("cum_x_range"), 0.0))
    y_range = abs(_as_float(metrics.get("cum_y_range"), 0.0))
    net_y = _as_float(metrics.get("net_y"), 0.0)
    net_y_abs = abs(net_y)
    straightness = _as_float(metrics.get("straightness_ratio"), 0.0)
    direction_flip_ratio = _as_float(metrics.get("direction_flip_ratio"), 0.0)
    jerk_p90 = _as_float(metrics.get("jerk_p90"), 0.0)
    residual_to_p95 = _as_float(metrics.get("residual_to_p95_ratio"), 0.0)
    sustained_run = min(pairs, max(6, int(math.ceil(pairs * 0.55))))
    sustained = active_ratio >= 0.75 and max_active_run >= sustained_run
    if not sustained:
        return None

    high_step_motion = p95 >= float(resolved["p95_hard_px"]) and hard_ratio >= 0.55

    # Smooth intentional camera moves (deliberate pans/tilts/sweeps) must not be
    # flagged. This gate protects BOTH shake and camera_drop: a careless 收机下坠
    # is a jittery/non-smooth vertical sink (low straightness, direction flips),
    # whereas a deliberate tilt or sweep is smooth (high straightness, few flips)
    # and is suppressed even when its vertical magnitude is large.
    dominant_axis = max(x_range, y_range)
    minor_axis = max(1.0, min(x_range, y_range))
    smooth_sweep = (
        dominant_axis >= 80.0
        and dominant_axis >= minor_axis * float(resolved["sweep_axis_ratio"])
        and straightness >= 0.65
        and direction_flip_ratio <= 0.32
    )
    smooth_camera_move = (
        straightness >= float(resolved["smooth_move_straightness"])
        and direction_flip_ratio <= float(resolved["smooth_move_flip_ratio"])
    ) or smooth_sweep

    vertical_drop = (
        y_range >= float(resolved["tail_y_range_hard_px"])
        and net_y_abs >= float(resolved["tail_net_y_hard_px"])
        and y_range >= max(25.0, x_range * 1.25)
    )
    tail_weighted_drop = (
        is_tail
        and high_step_motion
        and net_y_abs >= float(resolved["tail_net_y_hard_px"]) * 0.75
        and y_range >= 55.0
        and y_range >= x_range
    )

    if (vertical_drop or tail_weighted_drop) and not smooth_camera_move:
        confidence = 0.86 + min(0.1, max(0.0, (y_range - 70.0) / 250.0))
        return {
            "event_type": QualityEventType.camera_drop.value,
            "risk_tier": "hard" if vertical_drop else "soft",
            "confidence": round(_clamp(confidence, 0.0, 0.96), 3),
            "severity": 0.88 if vertical_drop else 0.68,
            "description": (
                f"收机下坠（垂直累计{y_range:.1f}px、净下沉{net_y_abs:.1f}px、"
                f"p95位移{p95:.1f}px）"
            ),
        }

    jitter_like = (
        direction_flip_ratio >= float(resolved["jitter_flip_ratio"])
        or (
            jerk_p90 >= max(8.0, p95 * float(resolved["jitter_jerk_ratio"]))
            and straightness <= 0.78
        )
        or (
            residual_to_p95 >= 1.15
            and direction_flip_ratio >= max(0.12, float(resolved["jitter_flip_ratio"]) * 0.55)
        )
    )
    severe_jitter = (
        p95 >= float(resolved["p95_hard_px"]) + 2.0
        and hard_ratio >= 0.7
        and active_ratio >= 0.85
        and jitter_like
        and not smooth_camera_move
    )
    boundary_jitter = (
        (is_head or is_tail)
        and p95 >= float(resolved["p95_hard_px"])
        and hard_ratio >= 0.55
        and active_ratio >= 0.75
        and jitter_like
        and not smooth_camera_move
    )

    if severe_jitter or boundary_jitter:
        hard = severe_jitter
        confidence = 0.82 + min(0.12, max(0.0, (p95 - float(resolved["p95_hard_px"])) / 50.0))
        return {
            "event_type": QualityEventType.shake.value,
            "risk_tier": "hard" if hard else "soft",
            "confidence": round(_clamp(confidence, 0.0, 0.96), 3),
            "severity": 0.82 if hard else 0.58,
            "description": (
                f"镜头剧烈抖动（p95位移{p95:.1f}px、方向翻转率"
                f"{direction_flip_ratio:.2f}）"
            ),
        }

    return None


def refine_drop_window(
    dys: Sequence[float],
    window_start: float,
    step: float,
    *,
    thresholds: Mapping[str, Any] | None = None,
) -> tuple[float, float] | None:
    """Refine a camera-drop window to the sustained downward-motion subrange."""
    resolved = _resolve_thresholds(thresholds)
    values = [_as_float(value, 0.0) for value in dys]
    if len(values) < 4 or step <= 0:
        return None

    net_y = sum(values)
    if abs(net_y) < max(3.0, float(resolved["active_px"]) * 2.0):
        return None

    direction = 1.0 if net_y >= 0 else -1.0
    directional = [direction * value for value in values]
    positive_steps = [value for value in directional if value > 0]
    if len(positive_steps) < 3:
        return None

    directional_threshold = max(
        0.75,
        float(resolved["active_px"]) * 0.45,
        _percentile(positive_steps, 45) * 0.35,
    )
    motion_threshold = max(1.0, float(resolved["active_px"]) * 0.7)
    flags = [
        value >= directional_threshold and abs(value) >= motion_threshold for value in directional
    ]
    if not any(flags):
        return None

    filled = list(flags)
    for idx in range(1, len(filled) - 1):
        if not filled[idx] and filled[idx - 1] and filled[idx + 1]:
            filled[idx] = True

    runs = _true_runs(filled)
    if not runs:
        return None

    fps = 1.0 / step
    window_end = float(window_start) + len(values) * step
    min_pairs = max(3, int(round(fps * 0.28)))
    tail_bias_start = window_end - min(0.5, max(0.1, (window_end - window_start) * 0.25))
    scored_runs: list[tuple[float, int, int, float]] = []
    for left, right in runs:
        run_pairs = right - left + 1
        displacement = sum(max(0.0, value) for value in directional[left : right + 1])
        if run_pairs < min_pairs and displacement < max(8.0, abs(net_y) * 0.18):
            continue
        run_end_time = float(window_start) + (right + 1) * step
        recency_bonus = 1.0 if run_end_time >= tail_bias_start else 0.0
        score = displacement + recency_bonus * max(12.0, abs(net_y) * 0.25) + run_pairs * 0.2
        scored_runs.append((score, left, right, displacement))
    if not scored_runs:
        return None

    _score, left, right, _displacement = max(scored_runs, key=lambda item: item[0])
    refined_start = max(float(window_start), float(window_start) + left * step - 0.5 * step)
    refined_end = min(window_end, float(window_start) + (right + 1) * step + 0.5 * step)
    if window_end - refined_end <= max(0.18, 2.5 * step):
        refined_end = window_end

    min_duration = min(max(0.4, window_end - float(window_start)), float(resolved["refine_min_duration"]))
    if refined_end - refined_start < min_duration:
        deficit = min_duration - (refined_end - refined_start)
        refined_start = max(float(window_start), refined_start - deficit)
        if refined_end - refined_start < min_duration:
            refined_end = min(window_end, refined_end + (min_duration - (refined_end - refined_start)))

    if refined_end - refined_start < 0.35:
        return None

    refined_start = max(float(window_start), _floor_time(refined_start, resolved))
    refined_end = min(window_end, _ceil_time(refined_end, resolved))
    if refined_end <= refined_start:
        return (round(float(window_start), _TIME_DECIMALS), round(window_end, _TIME_DECIMALS))
    return (round(refined_start, _TIME_DECIMALS), round(refined_end, _TIME_DECIMALS))


def merge_adjacent_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge overlapping/touching same-type motion events."""
    normalized = [
        dict(event)
        for event in events
        if _as_float(event.get("end"), 0.0) > _as_float(event.get("start"), 0.0)
    ]
    normalized.sort(key=lambda event: (_as_float(event.get("start"), 0.0), str(event.get("event_type"))))
    merged: list[dict[str, Any]] = []
    for event in normalized:
        if (
            merged
            and event.get("event_type") == merged[-1].get("event_type")
            and _as_float(event.get("start"), 0.0) <= _as_float(merged[-1].get("end"), 0.0) + _MERGE_GAP_SEC
        ):
            current = merged[-1]
            current["end"] = round(
                max(_as_float(current.get("end"), 0.0), _as_float(event.get("end"), 0.0)),
                _TIME_DECIMALS,
            )
            current["start"] = round(
                min(_as_float(current.get("start"), 0.0), _as_float(event.get("start"), 0.0)),
                _TIME_DECIMALS,
            )
            current["confidence"] = max(
                _as_float(current.get("confidence"), 0.0),
                _as_float(event.get("confidence"), 0.0),
            )
            current["severity"] = max(
                _as_float(current.get("severity"), 0.0),
                _as_float(event.get("severity"), 0.0),
            )
            if event.get("risk_tier") == "hard":
                current["risk_tier"] = "hard"
            continue
        merged.append(dict(event))
    return merged


def detect_motion_events(
    video_path: str,
    *,
    sample_fps: float | None = None,
    width: int | None = None,
    window_sec: float | None = None,
    hop_sec: float | None = None,
    active_px: float | None = None,
    hard_px: float | None = None,
    p95_hard_px: float | None = None,
    tail_y_range_hard_px: float | None = None,
    tail_net_y_hard_px: float | None = None,
    smooth_move_straightness: float | None = None,
    smooth_move_flip_ratio: float | None = None,
    sweep_axis_ratio: float | None = None,
    jitter_flip_ratio: float | None = None,
    jitter_jerk_ratio: float | None = None,
    refine_min_duration: float | None = None,
    refine_round_sec: float | None = None,
) -> list[dict[str, Any]]:
    """Detect camera shake/drop quality events, fail-open on all IO failures."""
    if not video_path or not os.path.exists(video_path):
        logger.debug("[motion_guard] video not found, returning empty: %s", video_path)
        return []

    overrides = {
        "sample_fps": sample_fps,
        "width": width,
        "window_sec": window_sec,
        "hop_sec": hop_sec,
        "active_px": active_px,
        "hard_px": hard_px,
        "p95_hard_px": p95_hard_px,
        "tail_y_range_hard_px": tail_y_range_hard_px,
        "tail_net_y_hard_px": tail_net_y_hard_px,
        "smooth_move_straightness": smooth_move_straightness,
        "smooth_move_flip_ratio": smooth_move_flip_ratio,
        "sweep_axis_ratio": sweep_axis_ratio,
        "jitter_flip_ratio": jitter_flip_ratio,
        "jitter_jerk_ratio": jitter_jerk_ratio,
        "refine_min_duration": refine_min_duration,
        "refine_round_sec": refine_round_sec,
    }
    thresholds = _resolve_thresholds(overrides)
    try:
        cv2, np = _load_cv2_numpy()
        if cv2 is None or np is None:
            return []
        frames, duration = _read_motion_frames(
            cv2,
            np,
            video_path,
            sample_fps=max(1.0, float(thresholds["sample_fps"])),
            width=max(1, int(thresholds["width"])),
        )
        if len(frames) < 2:
            return []

        pair_estimates: list[dict[str, float]] = []
        for (previous_time, previous), (current_time, current) in zip(frames, frames[1:]):
            estimate = _estimate_pair(cv2, np, previous, current)
            if estimate is None:
                continue
            estimate["time"] = float(current_time)
            estimate["previous_time"] = float(previous_time)
            pair_estimates.append(estimate)
        if not pair_estimates:
            return []

        total_duration = max(float(duration), frames[-1][0] + 1.0 / float(thresholds["sample_fps"]))
        events = _classify_time_axis(pair_estimates, total_duration, thresholds)
    except Exception as exc:  # pragma: no cover - local codec/dependency failures
        logger.debug("[motion_guard] detection failed-open for %s: %s", video_path, exc)
        return []

    return merge_adjacent_events(events)


def _classify_time_axis(
    pair_estimates: Sequence[Mapping[str, float]],
    total_duration: float,
    thresholds: Mapping[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    window_sec = max(0.1, float(thresholds["window_sec"]))
    hop_sec = max(0.05, float(thresholds["hop_sec"]))
    sample_fps = max(1.0, float(thresholds["sample_fps"]))
    start = 0.0
    while start < total_duration:
        end = min(total_duration, start + window_sec)
        selected = [
            estimate
            for estimate in pair_estimates
            if start < float(estimate.get("time", 0.0)) <= end
        ]
        if selected:
            metrics = summarize_window(selected, thresholds=thresholds)
            metrics["duration_sec"] = round(end - start, _TIME_DECIMALS)
            event = classify_window(
                metrics,
                thresholds=thresholds,
                is_head=start <= 0.12,
                is_tail=end >= max(0.0, total_duration - 0.12),
            )
            if event:
                event_start, event_end = start, end
                if event["event_type"] == QualityEventType.camera_drop.value:
                    refined = refine_drop_window(
                        [float(item.get("dy", 0.0)) for item in selected],
                        start,
                        1.0 / sample_fps,
                        thresholds=thresholds,
                    )
                    if refined and start <= refined[0] < refined[1] <= end:
                        event_start, event_end = refined
                assembled = _build_event(event, metrics, event_start, event_end)
                if assembled["end"] > assembled["start"]:
                    events.append(assembled)
        if end >= total_duration:
            break
        start = round(start + hop_sec, _TIME_DECIMALS)
    events.sort(key=lambda event: event["start"])
    return events


def _build_event(
    fragment: Mapping[str, Any],
    metrics: Mapping[str, Any],
    start: float,
    end: float,
) -> dict[str, Any]:
    event_type = str(fragment["event_type"])
    rounded_start = round(float(start), _TIME_DECIMALS)
    rounded_end = round(float(end), _TIME_DECIMALS)
    p95 = _as_float(metrics.get("mag_p95"), 0.0)
    flip = _as_float(metrics.get("direction_flip_ratio"), 0.0)
    y_range = _as_float(metrics.get("cum_y_range"), 0.0)
    if event_type == QualityEventType.camera_drop.value:
        description = (
            f"sensor(motion_guard): 收机下坠 {rounded_start:.2f}~{rounded_end:.2f}s"
            f"（垂直累计{y_range:.1f}px、p95位移{p95:.1f}px）"
        )
    else:
        description = (
            f"sensor(motion_guard): 镜头剧烈抖动 {rounded_start:.2f}~{rounded_end:.2f}s"
            f"（p95位移{p95:.1f}px、方向翻转率{flip:.2f}）"
        )
    return {
        "event_type": event_type,
        "start": rounded_start,
        "end": rounded_end,
        "risk_tier": str(fragment.get("risk_tier") or "hard"),
        "confidence": _as_float(fragment.get("confidence"), 0.0),
        "severity": _as_float(fragment.get("severity"), 0.0),
        "source": "motion_guard",
        "description": description,
    }


def _read_motion_frames(
    cv2_module,
    np_module,
    video_path: str,
    *,
    sample_fps: float,
    width: int,
) -> tuple[list[tuple[float, Any]], float]:
    cap = cv2_module.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        return [], 0.0
    try:
        original_width = float(cap.get(cv2_module.CAP_PROP_FRAME_WIDTH) or 0.0)
        original_height = float(cap.get(cv2_module.CAP_PROP_FRAME_HEIGHT) or 0.0)
        source_fps = float(cap.get(cv2_module.CAP_PROP_FPS) or 0.0)
        frame_count = float(cap.get(cv2_module.CAP_PROP_FRAME_COUNT) or 0.0)
    finally:
        cap.release()

    if original_width <= 0 or original_height <= 0:
        return [], 0.0

    target_width = int(width)
    target_height = max(2, int(round(original_height * target_width / original_width)))
    if target_height % 2:
        target_height += 1
    duration = round(frame_count / source_fps, _TIME_DECIMALS) if source_fps > 0 else 0.0
    vf = f"fps={sample_fps:.3f},scale={target_width}:-2,format=gray"
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(30, int(math.ceil(max(duration, 1.0) * 10.0 + 20.0))),
        )
    except Exception as exc:
        logger.debug("[motion_guard] ffmpeg decode failed for %s: %s", video_path, exc)
        return [], duration
    if result.returncode != 0 or not result.stdout:
        logger.debug(
            "[motion_guard] ffmpeg decode returned %s for %s: %s",
            result.returncode,
            video_path,
            (result.stderr or b"")[-400:].decode(errors="ignore"),
        )
        return [], duration

    frame_size = target_width * target_height
    frame_count = len(result.stdout) // frame_size
    if frame_count <= 0:
        return [], duration

    raw = np_module.frombuffer(result.stdout[: frame_count * frame_size], dtype=np_module.uint8)
    decoded = raw.reshape((frame_count, target_height, target_width))
    frames: list[tuple[float, Any]] = []
    for idx, gray in enumerate(decoded):
        time_sec = round(idx / sample_fps, _TIME_DECIMALS)
        blurred = cv2_module.GaussianBlur(gray, (3, 3), 0)
        frames.append((time_sec, blurred))
    return frames, duration


def _estimate_pair(cv2_module, np_module, previous, current) -> dict[str, float] | None:
    height, width = previous.shape[:2]
    if width <= 0 or height <= 0:
        return None

    mask = np_module.zeros((height, width), np_module.uint8)
    side_width = max(8, int(width * 0.32))
    mask[:, :side_width] = 255
    mask[:, width - side_width :] = 255
    mask[: max(4, int(height * 0.18)), :] = 255

    points0 = cv2_module.goodFeaturesToTrack(
        previous,
        maxCorners=700,
        qualityLevel=0.01,
        minDistance=6,
        blockSize=7,
        mask=mask,
    )
    if points0 is None or len(points0) < 30:
        points0 = cv2_module.goodFeaturesToTrack(
            previous,
            maxCorners=700,
            qualityLevel=0.01,
            minDistance=6,
            blockSize=7,
        )
    if points0 is None or len(points0) < 12:
        return None

    points1, status, _err = cv2_module.calcOpticalFlowPyrLK(
        previous,
        current,
        points0,
        None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=(
            cv2_module.TERM_CRITERIA_EPS | cv2_module.TERM_CRITERIA_COUNT,
            30,
            0.01,
        ),
    )
    if points1 is None or status is None:
        return None

    good0 = points0[status.ravel() == 1].reshape(-1, 2)
    good1 = points1[status.ravel() == 1].reshape(-1, 2)
    if len(good0) < 12:
        return None

    matrix, inliers = cv2_module.estimateAffinePartial2D(
        good0,
        good1,
        method=cv2_module.RANSAC,
        ransacReprojThreshold=2.0,
        maxIters=2000,
        confidence=0.99,
    )
    if matrix is None:
        flow = good1 - good0
        dx, dy = np_module.median(flow, axis=0)
        rotation = 0.0
        inlier_ratio = 0.0
    else:
        dx = float(matrix[0, 2])
        dy = float(matrix[1, 2])
        rotation = math.degrees(math.atan2(float(matrix[1, 0]), float(matrix[0, 0])))
        inlier_ratio = float(inliers.mean()) if inliers is not None else 0.0

    return {
        "dx": float(dx),
        "dy": float(dy),
        "rot_deg": float(rotation),
        "mag": float(math.hypot(float(dx), float(dy))),
        "inlier_ratio": inlier_ratio,
        "tracks": float(len(good0)),
    }


def _resolve_thresholds(
    thresholds: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    try:
        resolved = build_settings().motion_guard.model_dump()
    except Exception:
        resolved = MotionGuardSettings().model_dump()
    if thresholds:
        resolved.update({key: value for key, value in thresholds.items() if value is not None})
    resolved.update({key: value for key, value in overrides.items() if value is not None})
    return resolved


def _load_cv2_numpy():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency missing
        logger.debug("[motion_guard] cv2/numpy unavailable, returning empty: %s", exc)
        return None, None
    return cv2, np


def _coerce_pair(pair: tuple[float, float, float] | Mapping[str, Any]) -> tuple[float, float, float]:
    if isinstance(pair, Mapping):
        return (
            _as_float(pair.get("dx"), 0.0),
            _as_float(pair.get("dy"), 0.0),
            _as_float(pair.get("rot_deg"), 0.0),
        )
    if len(pair) < 3:
        return (0.0, 0.0, 0.0)
    return (_as_float(pair[0], 0.0), _as_float(pair[1], 0.0), _as_float(pair[2], 0.0))


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (float(percentile) / 100.0)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _median(values: Sequence[float]) -> float:
    return _percentile(values, 50)


def _cumsum(values: Sequence[float]) -> list[float]:
    total = 0.0
    out: list[float] = []
    for value in values:
        total += float(value)
        out.append(total)
    return out


def _max_true_run(flags: Sequence[bool]) -> int:
    longest = 0
    current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def _true_runs(flags: Sequence[bool]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for idx, flag in enumerate(flags):
        if flag and run_start is None:
            run_start = idx
        elif not flag and run_start is not None:
            runs.append((run_start, idx - 1))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(flags) - 1))
    return runs


def _direction_flip_stats(values: Sequence[float]) -> tuple[int, int]:
    abs_values = [abs(float(value)) for value in values]
    threshold = max(0.35, _percentile(abs_values, 60) * 0.35)
    signs = [
        1 if value > 0 else -1
        for value in values
        if abs(float(value)) >= threshold and float(value) != 0.0
    ]
    if len(signs) < 2:
        return 0, len(signs)
    flips = sum(1 for idx in range(1, len(signs)) if signs[idx] * signs[idx - 1] < 0)
    return flips, len(signs)


def _floor_time(value: float, thresholds: Mapping[str, Any]) -> float:
    step = max(0.05, float(thresholds["refine_round_sec"]))
    return round(math.floor(float(value) / step) * step, _TIME_DECIMALS)


def _ceil_time(value: float, thresholds: Mapping[str, Any]) -> float:
    step = max(0.05, float(thresholds["refine_round_sec"]))
    return round(math.ceil(float(value) / step) * step, _TIME_DECIMALS)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
