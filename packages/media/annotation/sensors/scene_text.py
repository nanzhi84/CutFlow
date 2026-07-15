"""Scene-text sensor: locate burned-in / in-scene text regions in a frame.

Why: scene-text annotations are reusable evidence for video-layout safety checks
that is already in the shot (shop signs, posters, product labels, burned-in source
subtitles). The previous morphology heuristic (Sobel + close) fired on any
repetitive high-gradient texture — workshop ceiling beams, ventilation grilles,
fabric folds — flagging 30%+ of an ordinary frame as "text" and starving the
placement search. This sensor replaces it with the OpenCV Zoo PP-OCRv3 DB text
*detector* (geometry only, no recognition), which distinguishes real glyph
strokes from structural texture.

Sensor discipline mirrors :mod:`faces`: ``detect_scene_text_strict`` is the pure
core and *raises* when absence cannot be proven (detector unavailable / decode
failure), so a caption safety planner never mistakes a construction failure for
"no text here". Output is a list of axis-aligned normalized rects.

Calibration (OpenCV Zoo defaults, with a larger square input for 9:16 frames):
binaryThreshold=0.3, polygonThreshold=0.5, unclipRatio=2.0, maxCandidates=200,
input 960x960, mean=(122.68, 116.67, 104.01), scale=1/255. Deterministic: fixed
input size, no randomness.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SceneTextDetectionError(RuntimeError):
    """The scene-text detector could not produce a trustworthy result for a frame."""


class SceneTextDetectorUnavailable(SceneTextDetectionError):
    """The PP-OCRv3 DB detector cannot be constructed in this process."""


# PP-OCRv3 Chinese DB text-detection model bundled in the package (relative path).
_MODEL_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "text_detection_cn_ppocrv3_2023may.onnx"
)

# Square DB input. The Zoo card ships 736 for landscape stills; 9:16 talking-head
# frames (1080x1920) downscale too far at 736 and drop real posters, so we use
# 960 (a 32-multiple) which recovers them while still ignoring structural texture.
_INPUT_SIZE = 960
_BINARY_THRESHOLD = 0.3
_POLYGON_THRESHOLD = 0.5
_UNCLIP_RATIO = 2.0
_MAX_CANDIDATES = 200
_MEAN = (122.67891434, 116.66876762, 104.00698793)
_SCALE = 1.0 / 255.0

_detector = None
_detector_key: tuple | None = None
_warned_unavailable = False


def reset_detector_cache() -> None:
    """Clear the detector cache (for tests that monkeypatch the model path)."""
    global _detector, _detector_key, _warned_unavailable
    _detector = None
    _detector_key = None
    _warned_unavailable = False


def _warn_unavailable_once(reason: str) -> None:
    """Log detector unavailability once."""
    global _warned_unavailable
    if not _warned_unavailable:
        logger.warning(
            "[scene_text] scene-text sensor unavailable (%s); "
            "scene-text annotations will be unavailable. "
            "Ensure opencv>=4.8 and the PP-OCRv3 model exist.",
            reason,
        )
        _warned_unavailable = True


def _get_detector():
    """Lazily load and cache the DB detector; cv2/model unavailable returns None."""
    global _detector, _detector_key
    key = (str(_MODEL_PATH), _INPUT_SIZE)
    if _detector is not None and _detector_key == key:
        return _detector
    try:
        import cv2  # type: ignore
    except Exception as exc:  # pragma: no cover - cv2 missing
        _warn_unavailable_once(f"cv2 unavailable: {exc}")
        return None
    if not _MODEL_PATH.exists():
        _warn_unavailable_once(f"PP-OCRv3 model missing: {_MODEL_PATH}")
        return None
    try:
        det = cv2.dnn.TextDetectionModel_DB(str(_MODEL_PATH))
        det.setBinaryThreshold(_BINARY_THRESHOLD)
        det.setPolygonThreshold(_POLYGON_THRESHOLD)
        det.setUnclipRatio(_UNCLIP_RATIO)
        det.setMaxCandidates(_MAX_CANDIDATES)
        det.setInputParams(scale=_SCALE, size=(_INPUT_SIZE, _INPUT_SIZE), mean=_MEAN)
    except Exception as exc:  # pragma: no cover - model corrupt / cv2 too old
        _warn_unavailable_once(f"failed to create DB text detector: {exc}")
        return None
    _detector, _detector_key = det, key
    return det


def scene_text_detector_available() -> bool:
    """Whether the bundled DB text detector can be constructed in this process.

    ``detect_scene_text_strict`` raises when it cannot run, so caption placement
    uses this explicit capability probe (like ``face_detector_available``) to tell
    "no text present" apart from "detector unavailable" before trusting an empty
    result as proof that an anchor is safe.
    """

    return _get_detector() is not None


def detect_scene_text_strict(image) -> list[tuple[float, float, float, float]]:
    """Detect in-scene text regions or raise when their absence cannot be proven.

    ``image`` is a BGR array (``cv2.imread`` result). Returns each detected text
    region as an axis-aligned rect ``(x, y, w, h)`` normalized to [0, 1] (the
    bounding box of the DB detector's rotated quad, clamped into the frame).
    Recognition is intentionally unnecessary — geometry alone drives avoidance.
    """

    if image is None:
        raise SceneTextDetectionError("image is empty")
    det = _get_detector()
    if det is None:
        raise SceneTextDetectorUnavailable("scene-text detector is unavailable")
    try:
        height, width = int(image.shape[0]), int(image.shape[1])
        if height <= 0 or width <= 0:
            raise ValueError("image dimensions must be positive")
        boxes, _confidences = det.detect(image)
    except Exception as exc:
        raise SceneTextDetectionError(f"DB text detection failed: {exc}") from exc
    regions: list[tuple[float, float, float, float]] = []
    for quad in boxes or []:
        try:
            xs = [float(point[0]) for point in quad]
            ys = [float(point[1]) for point in quad]
        except (TypeError, ValueError, IndexError):
            continue
        left = max(0.0, min(xs)) / width
        top = max(0.0, min(ys)) / height
        right = min(float(width), max(xs)) / width
        bottom = min(float(height), max(ys)) / height
        if right <= left or bottom <= top:
            continue
        regions.append((left, top, right - left, bottom - top))
    return regions
