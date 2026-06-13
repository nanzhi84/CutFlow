"""Deterministic motion-guard sensor (camera drop / shake) at clip boundaries.

The motion-guard routines form a tightly-coupled cluster; they live on a small
``MotionGuard`` helper whose ``__init__`` takes the configuration the cluster
needs. Only portrait edge windows (head / tail) are scanned: an end-of-clip sink
(camera_drop) or sustained boundary jitter (shake) ruins lip-sync and should be
trimmed. Optical-flow + affine estimation yields per-pair motion metrics; the
classifier maps sustained motion to CAMERA_DROP (hard) or SHAKE (hard).

Fail-open: cv2/numpy/ffmpeg unavailable or a video that won't open yields no
frames -> no events. Returned events align QualityEventV4 (event_id added later).
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
from typing import Any

from packages.core.contracts import QualityEventType

from .._util import as_float, clamp

logger = logging.getLogger(__name__)


class MotionGuard:
    """Holds motion-guard configuration and the cohesive detection routines."""

    def __init__(
        self,
        *,
        portrait_edge_window: float = 2.0,
        motion_guard_enabled: bool = True,
        motion_guard_sample_fps: float = 12.0,
        motion_guard_width: int = 360,
        motion_guard_active_px: float = 1.5,
        motion_guard_hard_px: float = 3.0,
        motion_guard_p95_hard_px: float = 7.0,
        motion_guard_tail_y_range_hard_px: float = 70.0,
        motion_guard_tail_net_y_hard_px: float = 65.0,
        motion_guard_smooth_move_straightness: float = 0.88,
        motion_guard_smooth_move_flip_ratio: float = 0.16,
        motion_guard_sweep_axis_ratio: float = 2.3,
        motion_guard_jitter_flip_ratio: float = 0.22,
        motion_guard_jitter_jerk_ratio: float = 0.65,
        motion_guard_refine_min_duration: float = 0.8,
        motion_guard_refine_round_sec: float = 0.1,
    ) -> None:
        self.portrait_edge_window = portrait_edge_window
        self.motion_guard_enabled = motion_guard_enabled
        self.motion_guard_sample_fps = motion_guard_sample_fps
        self.motion_guard_width = motion_guard_width
        self.motion_guard_active_px = motion_guard_active_px
        self.motion_guard_hard_px = motion_guard_hard_px
        self.motion_guard_p95_hard_px = motion_guard_p95_hard_px
        self.motion_guard_tail_y_range_hard_px = motion_guard_tail_y_range_hard_px
        self.motion_guard_tail_net_y_hard_px = motion_guard_tail_net_y_hard_px
        self.motion_guard_smooth_move_straightness = motion_guard_smooth_move_straightness
        self.motion_guard_smooth_move_flip_ratio = motion_guard_smooth_move_flip_ratio
        self.motion_guard_sweep_axis_ratio = motion_guard_sweep_axis_ratio
        self.motion_guard_jitter_flip_ratio = motion_guard_jitter_flip_ratio
        self.motion_guard_jitter_jerk_ratio = motion_guard_jitter_jerk_ratio
        self.motion_guard_refine_min_duration = motion_guard_refine_min_duration
        self.motion_guard_refine_round_sec = motion_guard_refine_round_sec

    def motion_guard_windows(
        self, *, total_duration: float, video_type: str
    ) -> list[tuple[str, float, float]]:
        if video_type != "portrait" or total_duration <= 0.6:
            return []

        edge_window = min(total_duration, max(1.0, self.portrait_edge_window))
        windows: list[tuple[str, float, float]] = []
        head_end = min(total_duration, edge_window)
        if head_end >= 0.8:
            windows.append(("head", 0.0, round(head_end, 3)))

        tail_start = max(
            0.0, math.floor(max(0.0, total_duration - edge_window) * 10.0) / 10.0
        )
        if total_duration - tail_start >= 0.8 and (
            not windows or tail_start > windows[-1][2] - 0.12
        ):
            windows.append(("tail", round(tail_start, 3), round(total_duration, 3)))
        return windows

    def _read_motion_guard_frames(
        self,
        video_path: str,
        *,
        start: float,
        end: float,
    ) -> tuple[list[tuple[float, Any]], float]:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except Exception as exc:
            logger.debug("[motion] skipped, cv2/numpy unavailable: %s", exc)
            return [], 1.0

        if end <= start or not os.path.exists(video_path):
            return [], 1.0

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return [], 1.0

        frames: list[tuple[float, Any]] = []
        original_width = float(
            cap.get(cv2.CAP_PROP_FRAME_WIDTH) or self.motion_guard_width
        )
        original_height = float(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        cap.release()
        if original_width <= 0 or original_height <= 0:
            return [], 1.0

        scale_to_original = max(1.0, original_width / float(self.motion_guard_width or 1))
        target_width = int(self.motion_guard_width)
        target_height = max(
            2, int(round(original_height * target_width / original_width))
        )
        if target_height % 2:
            target_height += 1
        fps = max(1.0, self.motion_guard_sample_fps)
        duration = max(0.05, end - start)
        vf = f"fps={fps:.3f},scale={target_width}:{target_height},format=gray"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{duration:.3f}",
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
                timeout=max(12, int(math.ceil(duration * 8))),
            )
        except Exception as exc:
            logger.debug("[motion] ffmpeg decode failed for %s: %s", video_path, exc)
            return [], scale_to_original

        if result.returncode != 0 or not result.stdout:
            logger.debug(
                "[motion] ffmpeg decode returned %s for %s: %s",
                result.returncode,
                video_path,
                (result.stderr or b"")[-400:].decode(errors="ignore"),
            )
            return [], scale_to_original

        frame_size = target_width * target_height
        frame_count = len(result.stdout) // frame_size
        if frame_count <= 0:
            return [], scale_to_original

        raw = np.frombuffer(result.stdout[: frame_count * frame_size], dtype=np.uint8)
        decoded = raw.reshape((frame_count, target_height, target_width))
        for idx, gray in enumerate(decoded):
            t = start + idx / fps
            if t > end + 0.05:
                break
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            frames.append((round(t, 3), blurred))
        return frames, scale_to_original

    def _estimate_motion_guard_pair(
        self, previous: Any, current: Any
    ) -> dict[str, float] | None:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except Exception:
            return None

        height, width = previous.shape[:2]
        if width <= 0 or height <= 0:
            return None

        mask = np.zeros((height, width), np.uint8)
        side_width = max(8, int(width * 0.32))
        mask[:, :side_width] = 255
        mask[:, width - side_width :] = 255
        mask[: max(4, int(height * 0.18)), :] = 255

        points0 = cv2.goodFeaturesToTrack(
            previous,
            maxCorners=700,
            qualityLevel=0.01,
            minDistance=6,
            blockSize=7,
            mask=mask,
        )
        if points0 is None or len(points0) < 30:
            points0 = cv2.goodFeaturesToTrack(
                previous,
                maxCorners=700,
                qualityLevel=0.01,
                minDistance=6,
                blockSize=7,
            )
        if points0 is None or len(points0) < 12:
            return None

        points1, status, _err = cv2.calcOpticalFlowPyrLK(
            previous,
            current,
            points0,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        )
        if points1 is None or status is None:
            return None

        good0 = points0[status.ravel() == 1].reshape(-1, 2)
        good1 = points1[status.ravel() == 1].reshape(-1, 2)
        if len(good0) < 12:
            return None

        matrix, inliers = cv2.estimateAffinePartial2D(
            good0,
            good1,
            method=cv2.RANSAC,
            ransacReprojThreshold=2.0,
            maxIters=2000,
            confidence=0.99,
        )
        if matrix is None:
            flow = good1 - good0
            dx, dy = np.median(flow, axis=0)
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
            "mag": float(math.hypot(float(dx), float(dy))),
            "rot_deg": float(abs(rotation)),
            "tracks": float(len(good0)),
            "inlier_ratio": float(inlier_ratio),
        }

    def _floor_motion_guard_time(self, value: float) -> float:
        step = max(0.05, self.motion_guard_refine_round_sec)
        return round(math.floor(value / step) * step, 3)

    def _ceil_motion_guard_time(self, value: float) -> float:
        step = max(0.05, self.motion_guard_refine_round_sec)
        return round(math.ceil(value / step) * step, 3)

    def _refine_motion_guard_drop_window(
        self,
        *,
        start: float,
        end: float,
        pair_times: Any,
        dxs: Any,
        dys: Any,
        mags: Any,
    ) -> dict[str, Any] | None:
        try:
            import numpy as np  # type: ignore
        except Exception:
            return None

        if len(pair_times) < 4 or end <= start:
            return None

        net_y = float(np.sum(dys))
        if abs(net_y) < max(3.0, self.motion_guard_active_px * 2.0):
            return None

        direction = 1.0 if net_y >= 0 else -1.0
        directional_y = direction * dys
        positive_steps = directional_y[directional_y > 0]
        if len(positive_steps) < 3:
            return None

        directional_threshold = max(
            0.75,
            self.motion_guard_active_px * 0.45,
            float(np.percentile(positive_steps, 45)) * 0.35,
        )
        motion_threshold = max(1.0, self.motion_guard_active_px * 0.7)
        flags = (directional_y >= directional_threshold) & (mags >= motion_threshold)
        if not bool(np.any(flags)):
            return None

        filled = flags.copy()
        for idx in range(1, len(filled) - 1):
            if not filled[idx] and filled[idx - 1] and filled[idx + 1]:
                filled[idx] = True

        runs: list[tuple[int, int]] = []
        run_start: int | None = None
        for idx, flag in enumerate(filled):
            if bool(flag) and run_start is None:
                run_start = idx
            elif not bool(flag) and run_start is not None:
                runs.append((run_start, idx - 1))
                run_start = None
        if run_start is not None:
            runs.append((run_start, len(filled) - 1))
        if not runs:
            return None

        min_pairs = max(3, int(round(self.motion_guard_sample_fps * 0.28)))
        tail_bias_start = end - min(0.5, max(0.1, (end - start) * 0.25))
        scored_runs: list[tuple[float, int, int, float]] = []
        for left, right in runs:
            run_pairs = right - left + 1
            displacement = float(np.sum(np.maximum(0.0, directional_y[left : right + 1])))
            if run_pairs < min_pairs and displacement < max(8.0, abs(net_y) * 0.18):
                continue
            recency_bonus = 1.0 if float(pair_times[right]) >= tail_bias_start else 0.0
            score = (
                displacement
                + recency_bonus * max(12.0, abs(net_y) * 0.25)
                + run_pairs * 0.2
            )
            scored_runs.append((score, left, right, displacement))
        if not scored_runs:
            return None

        _score, left, right, displacement = max(scored_runs, key=lambda item: item[0])
        fps = max(1.0, self.motion_guard_sample_fps)
        refined_start = max(start, float(pair_times[left]) - 0.5 / fps)
        refined_end = min(end, float(pair_times[right]) + 0.5 / fps)
        if end - refined_end <= max(0.18, 2.5 / fps):
            refined_end = end

        min_duration = min(max(0.4, end - start), self.motion_guard_refine_min_duration)
        if refined_end - refined_start < min_duration:
            deficit = min_duration - (refined_end - refined_start)
            refined_start = max(start, refined_start - deficit)
            if refined_end - refined_start < min_duration:
                refined_end = min(
                    end, refined_end + (min_duration - (refined_end - refined_start))
                )

        if refined_end - refined_start < 0.35:
            return None

        refined_start = max(start, self._floor_motion_guard_time(refined_start))
        refined_end = min(end, self._ceil_motion_guard_time(refined_end))
        if refined_end <= refined_start:
            refined_start = round(start, 3)
            refined_end = round(end, 3)

        return {
            "refined_drop_start": refined_start,
            "refined_drop_end": refined_end,
            "refined_drop_duration": round(refined_end - refined_start, 3),
            "refined_drop_pairs": int(right - left + 1),
            "refined_drop_displacement_px360": round(displacement, 3),
        }

    def summarize_motion_guard_window(
        self,
        video_path: str,
        *,
        start: float,
        end: float,
    ) -> dict[str, Any]:
        try:
            import numpy as np  # type: ignore
        except Exception as exc:
            logger.debug("[motion] skipped, numpy unavailable: %s", exc)
            return {"start": round(start, 3), "end": round(end, 3), "frames": 0, "pairs": 0}

        frames, scale_to_original = self._read_motion_guard_frames(
            video_path, start=start, end=end
        )
        estimates: list[dict[str, float]] = []
        pair_times: list[float] = []
        for (_t0, previous), (t1, current) in zip(frames, frames[1:]):
            estimate = self._estimate_motion_guard_pair(previous, current)
            if estimate:
                estimates.append(estimate)
                pair_times.append(float(t1))

        if not estimates:
            return {
                "start": round(start, 3),
                "end": round(end, 3),
                "frames": len(frames),
                "pairs": 0,
                "scale_to_original": round(scale_to_original, 3),
            }

        mags = np.array([item["mag"] for item in estimates], dtype=float)
        dxs = np.array([item["dx"] for item in estimates], dtype=float)
        dys = np.array([item["dy"] for item in estimates], dtype=float)
        rotations = np.array([item["rot_deg"] for item in estimates], dtype=float)
        inliers = np.array([item["inlier_ratio"] for item in estimates], dtype=float)
        active = mags > self.motion_guard_active_px
        hard = mags > self.motion_guard_hard_px

        def _direction_flip_stats(values: Any) -> tuple[int, int]:
            abs_values = np.abs(values)
            threshold = max(0.35, float(np.percentile(abs_values, 60)) * 0.35)
            signs = np.sign(np.where(abs_values >= threshold, values, 0.0))
            signs = signs[signs != 0]
            if len(signs) < 2:
                return 0, int(len(signs))
            return int(np.sum(signs[1:] * signs[:-1] < 0)), int(len(signs))

        max_active_run = 0
        current_run = 0
        for flag in active:
            current_run = current_run + 1 if bool(flag) else 0
            max_active_run = max(max_active_run, current_run)

        cumulative_x = np.cumsum(dxs)
        cumulative_y = np.cumsum(dys)
        p95 = float(np.percentile(mags, 95))
        path_length = float(np.sum(mags))
        net_x = float(cumulative_x[-1])
        net_y = float(cumulative_y[-1])
        net_motion = float(math.hypot(net_x, net_y))
        straightness = net_motion / path_length if path_length > 0 else 0.0
        x_flips, x_direction_steps = _direction_flip_stats(dxs)
        y_flips, y_direction_steps = _direction_flip_stats(dys)
        direction_steps = max(1, x_direction_steps + y_direction_steps)
        direction_flip_ratio = float(x_flips + y_flips) / float(direction_steps)
        jerk = (
            np.hypot(np.diff(dxs), np.diff(dys))
            if len(dxs) > 1
            else np.array([0.0], dtype=float)
        )
        residual = np.hypot(dxs - float(np.median(dxs)), dys - float(np.median(dys)))
        metrics = {
            "start": round(start, 3),
            "end": round(end, 3),
            "frames": len(frames),
            "pairs": len(estimates),
            "scale_to_original": round(scale_to_original, 3),
            "mag_p50_px360": round(float(np.percentile(mags, 50)), 3),
            "mag_p90_px360": round(float(np.percentile(mags, 90)), 3),
            "mag_p95_px360": round(p95, 3),
            "mag_max_px360": round(float(np.max(mags)), 3),
            "mag_p95_original_px": round(p95 * scale_to_original, 3),
            "active_ratio": round(float(np.mean(active)), 3),
            "hard_ratio": round(float(np.mean(hard)), 3),
            "max_active_run_pairs": int(max_active_run),
            "cum_x_range_px360": round(
                float(np.max(cumulative_x) - np.min(cumulative_x)), 3
            ),
            "cum_y_range_px360": round(
                float(np.max(cumulative_y) - np.min(cumulative_y)), 3
            ),
            "path_length_px360": round(path_length, 3),
            "net_x_px360": round(net_x, 3),
            "net_y_px360": round(net_y, 3),
            "net_motion_px360": round(net_motion, 3),
            "straightness_ratio": round(straightness, 3),
            "x_direction_flips": x_flips,
            "y_direction_flips": y_flips,
            "direction_flip_ratio": round(direction_flip_ratio, 3),
            "jerk_p90_px360": round(float(np.percentile(jerk, 90)), 3),
            "residual_p90_px360": round(float(np.percentile(residual, 90)), 3),
            "residual_to_p95_ratio": round(
                float(np.percentile(residual, 90)) / (p95 + 1e-6), 3
            ),
            "rot_p95_deg": round(float(np.percentile(rotations, 95)), 4),
            "inlier_p50": round(float(np.percentile(inliers, 50)), 3),
        }
        refined_drop = self._refine_motion_guard_drop_window(
            start=start,
            end=end,
            pair_times=np.array(pair_times, dtype=float),
            dxs=dxs,
            dys=dys,
            mags=mags,
        )
        if refined_drop:
            metrics.update(refined_drop)
        return metrics

    def build_motion_guard_event_from_metrics(
        self,
        metrics: dict[str, Any],
        *,
        label: str,
        total_duration: float,
    ) -> dict[str, Any] | None:
        start = as_float(metrics.get("start"), 0.0)
        end = as_float(metrics.get("end"), start)
        duration = max(0.0, end - start)
        pairs = int(as_float(metrics.get("pairs"), 0.0))
        if duration < 0.8 or pairs < 8:
            return None

        p95 = as_float(metrics.get("mag_p95_px360"), 0.0)
        active_ratio = as_float(metrics.get("active_ratio"), 0.0)
        hard_ratio = as_float(metrics.get("hard_ratio"), 0.0)
        max_active_run = int(as_float(metrics.get("max_active_run_pairs"), 0.0))
        x_range = abs(as_float(metrics.get("cum_x_range_px360"), 0.0))
        y_range = abs(as_float(metrics.get("cum_y_range_px360"), 0.0))
        net_y = abs(as_float(metrics.get("net_y_px360"), 0.0))
        straightness = as_float(metrics.get("straightness_ratio"), 0.0)
        direction_flip_ratio = as_float(metrics.get("direction_flip_ratio"), 0.0)
        jerk_p90 = as_float(metrics.get("jerk_p90_px360"), 0.0)
        residual_to_p95 = as_float(metrics.get("residual_to_p95_ratio"), 0.0)
        is_tail = end >= max(0.0, total_duration - 0.12)
        is_head = start <= 0.12
        sustained = active_ratio >= 0.75 and max_active_run >= min(
            pairs, max(6, int(math.ceil(pairs * 0.55)))
        )
        high_step_motion = p95 >= self.motion_guard_p95_hard_px and hard_ratio >= 0.55
        vertical_drop = (
            y_range >= self.motion_guard_tail_y_range_hard_px
            and net_y >= self.motion_guard_tail_net_y_hard_px
            and y_range >= max(25.0, x_range * 1.25)
        )
        dominant_axis = max(x_range, y_range)
        minor_axis = max(1.0, min(x_range, y_range))
        smooth_sweep = (
            dominant_axis >= 80.0
            and dominant_axis >= minor_axis * self.motion_guard_sweep_axis_ratio
            and straightness >= 0.65
            and direction_flip_ratio <= 0.32
        )
        smooth_camera_move = (
            straightness >= self.motion_guard_smooth_move_straightness
            and direction_flip_ratio <= self.motion_guard_smooth_move_flip_ratio
        ) or smooth_sweep
        jitter_like = (
            direction_flip_ratio >= self.motion_guard_jitter_flip_ratio
            or (
                jerk_p90 >= max(8.0, p95 * self.motion_guard_jitter_jerk_ratio)
                and straightness <= 0.78
            )
            or (
                residual_to_p95 >= 1.15
                and direction_flip_ratio
                >= max(0.12, self.motion_guard_jitter_flip_ratio * 0.55)
            )
        )
        severe_jitter = (
            p95 >= self.motion_guard_p95_hard_px + 2.0
            and hard_ratio >= 0.7
            and active_ratio >= 0.85
            and jitter_like
            and not smooth_camera_move
        )

        if not sustained:
            return None

        if is_tail and (
            vertical_drop
            or (high_step_motion and y_range >= 55.0 and y_range >= x_range)
        ):
            event_type = QualityEventType.camera_drop.value
            description = (
                "motion_guard: sustained sink/recovery shake near the end, "
                f"p95 displacement {p95:.1f}px(360w), vertical cumulative {y_range:.1f}px, "
                "hurts lip-sync, recommend trimming."
            )
        elif (is_head or is_tail) and severe_jitter:
            event_type = QualityEventType.shake.value
            description = (
                "motion_guard: sustained jitter in the boundary window, "
                f"p95 displacement {p95:.1f}px(360w), active-frame ratio {active_ratio:.0%}, "
                "hurts main-track stability."
            )
        else:
            return None

        event_start = start
        event_end = end
        if event_type == QualityEventType.camera_drop.value:
            refined_start = as_float(metrics.get("refined_drop_start"), start)
            refined_end = as_float(metrics.get("refined_drop_end"), end)
            if (
                start <= refined_start < refined_end <= end
                and refined_end - refined_start >= 0.35
            ):
                event_start = refined_start
                event_end = refined_end

        confidence = clamp(
            0.82 + max(0.0, min(0.12, (p95 - self.motion_guard_p95_hard_px) / 50.0)),
            0.0,
            0.96,
        )
        return {
            "event_type": event_type,
            "risk_tier": "hard",
            "start": round(event_start, 3),
            "end": round(event_end, 3),
            "description": description,
            "severity": 0.88
            if event_type == QualityEventType.camera_drop.value
            else 0.82,
            "confidence": round(confidence, 3),
            "source": "motion_guard",
            "window_label": label,
            "metrics": metrics,
        }

    def detect_motion_guard_events(
        self,
        video_path: str,
        *,
        total_duration: float,
        video_type: str,
    ) -> dict[str, Any]:
        windows = self.motion_guard_windows(
            total_duration=total_duration, video_type=video_type
        )
        result: dict[str, Any] = {
            "enabled": bool(self.motion_guard_enabled),
            "windows": [],
            "events": [],
        }
        if not self.motion_guard_enabled or not windows:
            return result

        for label, start, end in windows:
            metrics = self.summarize_motion_guard_window(video_path, start=start, end=end)
            metrics["label"] = label
            result["windows"].append(metrics)
            event = self.build_motion_guard_event_from_metrics(
                metrics,
                label=label,
                total_duration=total_duration,
            )
            if event:
                result["events"].append(event)
        return result
