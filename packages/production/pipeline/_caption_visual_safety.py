"""Deterministic pixel safety analysis for emphasis-caption anchors.

The planner samples the final subtitle-free composite and proves an anchor safe
against three independent signals on every requested frame: YuNet faces, local
OpenCV text-like regions, and visual busyness.  Missing frames or unavailable
detectors fail closed; an empty detector result is only trusted after the
detector capability has been confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.media.annotation.sensors import detect_faces_strict, face_detector_available

_FACE_OVERLAP_MAX = 0.02
_SCENE_TEXT_OVERLAP_MAX = 0.04
_BUSY_SCORE_MAX = 0.72
_FACE_PADDING_RATIO = 0.12
_MAX_SAFE_ANCHORS = 6


@dataclass(frozen=True)
class VisualSafetyResult:
    anchors: list[dict]
    rejected_face: int = 0
    rejected_scene_text: int = 0
    rejected_busy: int = 0
    unavailable_detector: str | None = None

    @property
    def rejected_total(self) -> int:
        return self.rejected_face + self.rejected_scene_text + self.rejected_busy


@dataclass(frozen=True)
class OptionSafetyResult:
    options: list[dict]
    rejected_face: int = 0
    rejected_scene_text: int = 0
    rejected_busy: int = 0
    unavailable_detector: str | None = None

    @property
    def rejected_total(self) -> int:
        return self.rejected_face + self.rejected_scene_text + self.rejected_busy


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


def evaluate_anchor_safety(
    *,
    images: list[Any],
    sample_frames: list[int],
    anchors: list[dict],
) -> VisualSafetyResult:
    """Hard-filter anchor candidates using all three sampled final-video frames."""

    cv2, frame_observations, unavailable = _collect_frame_observations(
        images=images,
        sample_frames=sample_frames,
    )
    if unavailable is not None or cv2 is None:
        return VisualSafetyResult(anchors=[], unavailable_detector=unavailable)

    safe: list[tuple[tuple[float, float, float, str], dict]] = []
    rejected_face = 0
    rejected_text = 0
    rejected_busy = 0
    for anchor in anchors:
        rect = _rect_tuple(anchor.get("rect"))
        if rect is None:
            rejected_busy += 1
            continue
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
                return VisualSafetyResult(anchors=[], unavailable_detector="busy")
            busy_score = max(busy_score, score)

        if face_overlap > _FACE_OVERLAP_MAX:
            rejected_face += 1
            continue
        if text_overlap > _SCENE_TEXT_OVERLAP_MAX:
            rejected_text += 1
            continue
        if busy_score > _BUSY_SCORE_MAX:
            rejected_busy += 1
            continue

        anchor_id = str(anchor.get("anchor_id") or anchor.get("layout_box_id") or "")
        materialized = {
            "anchor_id": anchor_id,
            "rect": dict(anchor.get("rect") or {}),
            "text_align": str(anchor.get("text_align") or "center"),
            "max_lines": int(anchor.get("max_lines") or 1),
            "text_capacity": int(anchor.get("text_capacity") or 0),
            "allowed_enter_directions": list(anchor.get("allowed_enter_directions") or []),
            "face_overlap": round(face_overlap, 4),
            "scene_text_overlap": round(text_overlap, 4),
            "busy_score": round(busy_score, 4),
            "region_tags": [str(item) for item in (anchor.get("region_tags") or [])],
            "sample_frames": list(sample_frames),
        }
        safe.append(((face_overlap, text_overlap, busy_score, anchor_id), materialized))

    safe.sort(key=lambda item: item[0])
    return VisualSafetyResult(
        anchors=[item for _key, item in safe[:_MAX_SAFE_ANCHORS]],
        rejected_face=rejected_face,
        rejected_scene_text=rejected_text,
        rejected_busy=rejected_busy,
    )


def evaluate_option_safety(
    *,
    images: list[Any],
    sample_frames: list[int],
    option_candidates: list[dict],
) -> OptionSafetyResult:
    """Hard-filter every anchor+animation envelope on all three final frames."""

    cv2, frame_observations, unavailable = _collect_frame_observations(
        images=images,
        sample_frames=sample_frames,
    )
    if unavailable is not None or cv2 is None:
        return OptionSafetyResult(options=[], unavailable_detector=unavailable)

    safe: list[dict] = []
    rejected_face = 0
    rejected_text = 0
    rejected_busy = 0
    for option in option_candidates:
        envelope = _rect_tuple(option.get("safety_envelope"))
        if envelope is None:
            rejected_busy += 1
            continue
        face_overlap = 0.0
        text_overlap = 0.0
        busy_score = 0.0
        for faces, text_regions, image in frame_observations:
            face_overlap = max(
                face_overlap,
                max(
                    (_overlap_fraction(envelope, face) for face in faces),
                    default=0.0,
                ),
            )
            text_overlap = max(
                text_overlap,
                max(
                    (_overlap_fraction(envelope, region) for region in text_regions),
                    default=0.0,
                ),
            )
            score = _busy_score(cv2, image, envelope)
            if score is None:
                return OptionSafetyResult(options=[], unavailable_detector="busy")
            busy_score = max(busy_score, score)
        if face_overlap > _FACE_OVERLAP_MAX:
            rejected_face += 1
            continue
        if text_overlap > _SCENE_TEXT_OVERLAP_MAX:
            rejected_text += 1
            continue
        if busy_score > _BUSY_SCORE_MAX:
            rejected_busy += 1
            continue
        safe.append(
            {
                **option,
                "face_overlap": round(face_overlap, 4),
                "scene_text_overlap": round(text_overlap, 4),
                "busy_score": round(busy_score, 4),
                "sample_frames": list(sample_frames),
            }
        )
    return OptionSafetyResult(
        options=safe,
        rejected_face=rejected_face,
        rejected_scene_text=rejected_text,
        rejected_busy=rejected_busy,
    )


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
        import numpy as np  # type: ignore
    except Exception:
        return None, [], "opencv"
    if not face_detector_available():
        return None, [], "face"

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
        text_regions = _detect_text_like_regions(cv2, np, image)
        if text_regions is None:
            return None, [], "scene_text"
        observations.append((faces, text_regions, image))
    return cv2, observations, None


def _detect_text_like_regions(cv2, np, image) -> list[tuple[float, float, float, float]] | None:
    """Detect text-shaped occupied regions; recognition is intentionally unnecessary.

    Horizontal and vertical morphology covers common shop signs, labels, and
    burned-in source captions without a network model.  The result is geometry only.
    """

    try:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        regions: list[tuple[float, float, float, float]] = []
        for dx, dy, kernel_shape in ((1, 0, (17, 3)), (0, 1, (3, 17))):
            gradient = cv2.Sobel(gray, cv2.CV_32F, dx, dy, ksize=3)
            gradient = cv2.convertScaleAbs(gradient)
            gradient = cv2.GaussianBlur(gradient, (3, 3), 0)
            _threshold, mask = cv2.threshold(
                gradient, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU
            )
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, kernel_shape)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
            contours, _hierarchy = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            for contour in contours:
                x, y, box_w, box_h = cv2.boundingRect(contour)
                if not _text_region_shape_ok(x, y, box_w, box_h, width, height):
                    continue
                roi = mask[y : y + box_h, x : x + box_w]
                fill = float(np.count_nonzero(roi)) / float(max(1, roi.size))
                if fill < 0.08:
                    continue
                regions.append((x / width, y / height, box_w / width, box_h / height))
        return _merge_regions(regions)
    except Exception:
        return None


def _text_region_shape_ok(
    x: int,
    y: int,
    box_w: int,
    box_h: int,
    width: int,
    height: int,
) -> bool:
    del x, y
    if box_w < max(10, int(width * 0.035)) or box_h < max(6, int(height * 0.01)):
        return False
    area_ratio = (box_w * box_h) / float(max(1, width * height))
    if area_ratio > 0.30:
        return False
    aspect = box_w / float(max(1, box_h))
    inverse = box_h / float(max(1, box_w))
    return 1.15 <= aspect <= 25.0 or 1.15 <= inverse <= 25.0


def _merge_regions(
    regions: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    ordered = sorted(regions, key=lambda rect: (rect[1], rect[0], rect[2], rect[3]))
    merged: list[tuple[float, float, float, float]] = []
    for rect in ordered:
        if any(_overlap_fraction(rect, existing) >= 0.85 for existing in merged):
            continue
        merged.append(rect)
    return merged


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
