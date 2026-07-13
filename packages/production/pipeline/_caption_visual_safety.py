"""Deterministic pixel safety analysis for emphasis-caption anchors.

The planner samples the final subtitle-free composite and proves an anchor safe
against three independent signals on every requested frame: YuNet faces, the
PP-OCRv3 scene-text detector, and visual busyness.  Missing frames or unavailable
detectors fail closed; an empty detector result is only trusted after the
detector capability has been confirmed.

Emphasis captions carry a floor: to hit it, option safety is measured once per
window and then thresholded at graduated tiers.  The face gate never moves (it is
the immovable red line); only the scene-text and busyness thresholds relax, and
tier 3 falls back to the single least-busy face-clear option per event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.media.annotation.sensors import (
    detect_faces_strict,
    detect_scene_text_strict,
    face_detector_available,
    scene_text_detector_available,
)

# Face overlap is the immovable red line: it is identical across all relaxation
# tiers and is never widened to reach the emphasis floor.
_FACE_OVERLAP_MAX = 0.02

# Tier 1 (default) scene-text / busyness thresholds.
_SCENE_TEXT_OVERLAP_MAX = 0.04
_BUSY_SCORE_MAX = 0.72

# Tier 2 relaxed thresholds (face unchanged), applied only to events that still
# have no option after tier 1 and only while below the emphasis floor. Public so
# the planner can pass them and report them in the relaxation degradation.
EMPHASIS_TIER2_SCENE_TEXT_MAX = 0.12
EMPHASIS_TIER2_BUSY_MAX = 0.80

# Every finished video should carry at least this many emphasis-caption events
# when emphasis is enabled and candidates exist.
EMPHASIS_MIN_EVENTS = 5

_FACE_PADDING_RATIO = 0.12
@dataclass(frozen=True)
class OptionMeasurement:
    """One option candidate's measured overlaps on the sampled final frames.

    Metrics are threshold-independent, so a window is measured once and then
    re-thresholded per relaxation tier without re-running detection. ``valid`` is
    False when the option carried no usable safety envelope.
    """

    option: dict
    face_overlap: float
    scene_text_overlap: float
    busy_score: float
    valid: bool = True


@dataclass(frozen=True)
class OptionMeasurementResult:
    measurements: list[OptionMeasurement]
    sample_frames: list[int]
    unavailable_detector: str | None = None


def sample_frame_indices(start_frame: int, end_frame: int) -> list[int]:
    """Return distinct enter/mid/exit-before frame indices, else ``[]``.

    Three real observations are a hard requirement: very short windows that cannot
    supply three distinct frames are not eligible for visual caption options.
    """

    start = max(0, int(start_frame))
    end = max(start, int(end_frame))
    if end - start < 3:
        return []
    indices = [start, start + (end - start - 1) // 2, end - 1]
    return indices if len(set(indices)) == 3 else []


def measure_option_candidates(
    *,
    images: list[Any],
    sample_frames: list[int],
    option_candidates: list[dict],
) -> OptionMeasurementResult:
    """Measure each option's overlaps on all three final frames, once.

    The result is threshold-independent so the planner can apply graduated tiers
    (T1 default, T2 relaxed scene-text/busyness, T3 best face-clear option) without
    re-extracting frames or re-running detection.
    """

    cv2, frame_observations, unavailable = _collect_frame_observations(
        images=images,
        sample_frames=sample_frames,
    )
    if unavailable is not None or cv2 is None:
        return OptionMeasurementResult(
            measurements=[], sample_frames=list(sample_frames), unavailable_detector=unavailable
        )
    measurements: list[OptionMeasurement] = []
    for option in option_candidates:
        envelope = _rect_tuple(option.get("safety_envelope"))
        if envelope is None:
            measurements.append(
                OptionMeasurement(
                    option=option, face_overlap=1.0, scene_text_overlap=1.0,
                    busy_score=1.0, valid=False,
                )
            )
            continue
        overlaps = _measure_rect(cv2, frame_observations, envelope)
        if overlaps is None:
            return OptionMeasurementResult(
                measurements=[], sample_frames=list(sample_frames), unavailable_detector="busy"
            )
        face_overlap, text_overlap, busy_score = overlaps
        measurements.append(
            OptionMeasurement(
                option=option,
                face_overlap=face_overlap,
                scene_text_overlap=text_overlap,
                busy_score=busy_score,
            )
        )
    return OptionMeasurementResult(
        measurements=measurements, sample_frames=list(sample_frames)
    )


def select_options_at_thresholds(
    result: OptionMeasurementResult,
    *,
    face_max: float = _FACE_OVERLAP_MAX,
    scene_text_max: float = _SCENE_TEXT_OVERLAP_MAX,
    busy_max: float = _BUSY_SCORE_MAX,
) -> tuple[list[dict], int, int, int]:
    """Keep options passing all three gates at the given thresholds.

    Rejections are attributed to the first failing gate in face -> scene-text ->
    busyness order, matching the historical single-tier counting.
    """

    safe: list[dict] = []
    rejected_face = 0
    rejected_text = 0
    rejected_busy = 0
    for measurement in result.measurements:
        if not measurement.valid:
            rejected_busy += 1
            continue
        if measurement.face_overlap > face_max:
            rejected_face += 1
            continue
        if measurement.scene_text_overlap > scene_text_max:
            rejected_text += 1
            continue
        if measurement.busy_score > busy_max:
            rejected_busy += 1
            continue
        safe.append(_materialize_safe_option(measurement, result.sample_frames))
    return safe, rejected_face, rejected_text, rejected_busy


def select_best_face_clear_option(
    result: OptionMeasurementResult,
    *,
    face_max: float = _FACE_OVERLAP_MAX,
) -> dict | None:
    """Tier 3: the single least-busy face-clear option, ignoring scene-text/busy gates.

    Face overlap still hard-filters (the red line); among survivors the option with
    the smallest (scene_text_overlap, busy_score) wins, tie-broken by option id for
    determinism. ``None`` when no face-clear option exists.
    """

    eligible = [
        measurement
        for measurement in result.measurements
        if measurement.valid and measurement.face_overlap <= face_max
    ]
    if not eligible:
        return None
    best = min(
        eligible,
        key=lambda measurement: (
            measurement.scene_text_overlap,
            measurement.busy_score,
            str(measurement.option.get("caption_option_id") or ""),
        ),
    )
    return _materialize_safe_option(best, result.sample_frames)


def count_face_blocked(
    result: OptionMeasurementResult, *, face_max: float = _FACE_OVERLAP_MAX
) -> int:
    """How many measured options the face red line rejects (for tier-3 reporting)."""

    return sum(
        1
        for measurement in result.measurements
        if measurement.valid and measurement.face_overlap > face_max
    )


def _materialize_safe_option(measurement: OptionMeasurement, sample_frames: list[int]) -> dict:
    return {
        **measurement.option,
        "face_overlap": round(measurement.face_overlap, 4),
        "scene_text_overlap": round(measurement.scene_text_overlap, 4),
        "busy_score": round(measurement.busy_score, 4),
        "sample_frames": list(sample_frames),
    }


def _measure_rect(
    cv2,
    frame_observations: list[
        tuple[
            list[tuple[float, float, float, float]],
            list[tuple[float, float, float, float]],
            Any,
        ]
    ],
    rect: tuple[float, float, float, float],
) -> tuple[float, float, float] | None:
    """Max face / scene-text overlap and busyness for one rect over all frames.

    Returns ``None`` when busyness cannot be scored (a detector failure the caller
    surfaces as unavailable), mirroring the historical fail-closed behavior.
    """

    face_overlap = 0.0
    text_overlap = 0.0
    busy_score = 0.0
    for faces, text_regions, image in frame_observations:
        face_overlap = max(
            face_overlap,
            max((_overlap_fraction(rect, face) for face in faces), default=0.0),
        )
        text_overlap = max(
            text_overlap,
            max((_overlap_fraction(rect, region) for region in text_regions), default=0.0),
        )
        score = _busy_score(cv2, image, rect)
        if score is None:
            return None
        busy_score = max(busy_score, score)
    return face_overlap, text_overlap, busy_score


def _collect_frame_observations(
    *,
    images: list[Any],
    sample_frames: list[int],
) -> tuple[
    Any | None,
    list[
        tuple[
            list[tuple[float, float, float, float]],
            list[tuple[float, float, float, float]],
            Any,
        ]
    ],
    str | None,
]:
    if len(images) != 3 or len(sample_frames) != 3:
        return None, [], "frame_extraction"
    try:
        import cv2  # type: ignore
    except Exception:
        return None, [], "opencv"
    if not face_detector_available():
        return None, [], "face"
    if not scene_text_detector_available():
        return None, [], "scene_text"

    observations = []
    for image in images:
        if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
            return None, [], "frame_decode"
        height, width = int(image.shape[0]), int(image.shape[1])
        if width <= 0 or height <= 0:
            return None, [], "frame_decode"
        try:
            faces = [
                _normalize_padded_bbox(face.bbox, width, height)
                for face in detect_faces_strict(image)
            ]
        except Exception:
            return None, [], "face"
        try:
            text_regions = list(detect_scene_text_strict(image))
        except Exception:
            return None, [], "scene_text"
        observations.append((faces, text_regions, image))
    return cv2, observations, None


def _busy_score(cv2, image, rect: tuple[float, float, float, float]) -> float | None:
    try:
        height, width = image.shape[:2]
        x, y, box_w, box_h = rect
        x0 = max(0, min(width - 1, int(x * width)))
        y0 = max(0, min(height - 1, int(y * height)))
        x1 = max(x0 + 1, min(width, int((x + box_w) * width)))
        y1 = max(y0 + 1, min(height, int((y + box_h) * height)))
        crop = image[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 80, 180)
        edge_density = float((edges > 0).mean())
        contrast = float(gray.std()) / 128.0
        return round(
            min(1.0, edge_density / 0.18) * 0.75
            + min(1.0, contrast) * 0.25,
            4,
        )
    except Exception:
        return None


def _normalize_padded_bbox(
    bbox: tuple[float, float, float, float], width: int, height: int
) -> tuple[float, float, float, float]:
    x, y, box_w, box_h = bbox
    pad_x = box_w * _FACE_PADDING_RATIO
    pad_y = box_h * _FACE_PADDING_RATIO
    left = max(0.0, x - pad_x)
    top = max(0.0, y - pad_y)
    right = min(float(width), x + box_w + pad_x)
    bottom = min(float(height), y + box_h + pad_y)
    return left / width, top / height, (right - left) / width, (bottom - top) / height


def _rect_tuple(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        x = float(value.get("x"))
        y = float(value.get("y"))
        width = float(value.get("w"))
        height = float(value.get("h"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > 1 or y + height > 1:
        return None
    return x, y, width, height


def _overlap_fraction(
    anchor: tuple[float, float, float, float],
    occupied: tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = anchor
    bx, by, bw, bh = occupied
    overlap_w = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_h = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    return (overlap_w * overlap_h) / max(aw * ah, 1e-9)
