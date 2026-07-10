"""Multi-face sensor: count faces present in a frame (incl. mirror/reflection/screen/background).

Why: digital-human (lip-sync) nodes hard-reject a driving video that "contains
multiple faces". Talking-head recorded against mirrored wardrobes / glossy glass
reflects a "second face" the VLM may miss, so those windows get wrongly selected
to drive lip-sync and the whole task fails. This sensor deterministically counts
faces with OpenCV YuNet as the authoritative source (no VLM dependency).

Sensor discipline (same as cv_quality): ``count_faces_in_image`` (BGR image ->
face count) is the pure core; fail-open - cv2 unavailable / model missing /
decode failure returns 0 (no negative evidence; never misclassify a single
speaker window as multi-face).

Calibration: score_threshold=0.6 + min_face_frac=0.05.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FaceDetection:
    """One YuNet detection: bounding box, score, and the 5 facial landmarks
    (right eye, left eye, nose tip, right mouth corner, left mouth corner)."""

    bbox: tuple[float, float, float, float]  # x, y, w, h
    score: float
    landmarks: tuple[tuple[float, float], ...]  # 5 (x, y) points


class FaceDetectionError(RuntimeError):
    """YuNet could not produce a trustworthy result for one frame."""


class FaceDetectorUnavailable(FaceDetectionError):
    """YuNet cannot be constructed in this process."""

# YuNet face-detection model bundled in the package (package-relative path).
_MODEL_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "face_detection_yunet_2023mar.onnx"
)

_DEFAULT_SCORE = 0.6
_DEFAULT_MIN_FACE_FRAC = 0.05

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
    """Warn once when the detector is unavailable.

    The gate silently no-ops (all windows count 0) when unavailable, so it must
    be visible in the log rather than buried at debug level.
    """
    global _warned_unavailable
    if not _warned_unavailable:
        logger.warning(
            "[faces] multi-face sensor unavailable (%s); the multi-face gate will "
            "silently no-op (all windows face_count=0, mirror-reflection talking-head "
            "windows won't be blocked from lip-sync). Ensure opencv>=4.8 and the YuNet "
            "model exist.",
            reason,
        )
        _warned_unavailable = True


def _get_detector(score_threshold: float):
    """Lazily load and cache the YuNet detector; cv2/model unavailable returns None."""
    global _detector, _detector_key
    key = (str(_MODEL_PATH), round(float(score_threshold), 3))
    if _detector is not None and _detector_key == key:
        return _detector
    try:
        import cv2  # type: ignore
    except Exception as exc:  # pragma: no cover - cv2 missing
        _warn_unavailable_once(f"cv2 unavailable: {exc}")
        return None
    if not _MODEL_PATH.exists():
        _warn_unavailable_once(f"YuNet model missing: {_MODEL_PATH}")
        return None
    try:
        det = cv2.FaceDetectorYN.create(
            str(_MODEL_PATH),
            "",
            (320, 320),
            score_threshold=float(score_threshold),
            nms_threshold=0.3,
            top_k=50,
        )
    except Exception as exc:  # pragma: no cover - model corrupt / cv2 too old
        _warn_unavailable_once(f"failed to create YuNet detector: {exc}")
        return None
    _detector, _detector_key = det, key
    return det


def face_detector_available(*, score_threshold: float = _DEFAULT_SCORE) -> bool:
    """Whether the bundled YuNet detector can be constructed in this process.

    ``detect_faces`` intentionally fails open to an empty list, which is correct for
    annotation gates but cannot tell a safety planner whether ``[]`` means "no face"
    or "detector unavailable".  Caption placement uses this explicit capability
    probe so it never treats missing detection as proof that an anchor is safe.
    """

    return _get_detector(score_threshold) is not None


def detect_faces(
    image,
    *,
    score_threshold: float = _DEFAULT_SCORE,
) -> list[FaceDetection]:
    """Detect faces in a BGR image (cv2.imread result), exposing each detection's
    bounding box, score, and 5 landmarks.

    Unlike ``count_faces_in_image`` this applies no ``min_face_frac`` gate — the
    caller decides which detections qualify. fail-open: empty image / detector
    unavailable / detect failure returns ``[]`` (no negative evidence).
    """
    try:
        return detect_faces_strict(image, score_threshold=score_threshold)
    except Exception as exc:  # fail-open API retained for annotation gates
        logger.debug("[faces] detect failed: %s", exc)
        return []


def detect_faces_strict(
    image,
    *,
    score_threshold: float = _DEFAULT_SCORE,
) -> list[FaceDetection]:
    """Detect faces or raise when absence cannot be proven.

    Caption placement is a pixel-safety boundary, so it must distinguish a real
    empty result from a detector construction/runtime failure. Annotation callers
    keep using :func:`detect_faces`, whose historical fail-open behavior is
    intentionally unchanged.
    """

    if image is None:
        raise FaceDetectionError("image is empty")
    det = _get_detector(score_threshold)
    if det is None:
        raise FaceDetectorUnavailable("YuNet detector is unavailable")
    try:
        h, w = int(image.shape[0]), int(image.shape[1])
        if h <= 0 or w <= 0:
            raise ValueError("image dimensions must be positive")
        det.setInputSize((w, h))
        _, faces = det.detect(image)
    except Exception as exc:
        raise FaceDetectionError(f"YuNet detection failed: {exc}") from exc
    if faces is None:
        return []
    try:
        detections: list[FaceDetection] = []
        for f in faces:
            # YuNet row: [x, y, w, h, x_re, y_re, x_le, y_le, x_nt, y_nt,
            #             x_rcm, y_rcm, x_lcm, y_lcm, score]
            landmarks = tuple(
                (float(f[4 + 2 * i]), float(f[5 + 2 * i])) for i in range(5)
            )
            detections.append(
                FaceDetection(
                    bbox=(float(f[0]), float(f[1]), float(f[2]), float(f[3])),
                    score=float(f[14]),
                    landmarks=landmarks,
                )
            )
        return detections
    except Exception as exc:
        raise FaceDetectionError(f"invalid YuNet result: {exc}") from exc


def count_faces_in_image(
    image,
    *,
    score_threshold: float = _DEFAULT_SCORE,
    min_face_frac: float = _DEFAULT_MIN_FACE_FRAC,
) -> int:
    """Count faces in a BGR image (cv2.imread result); only faces with both sides
    >= short_side * min_face_frac.

    fail-open: empty image / detector unavailable / detect failure returns 0.
    """
    if image is None:
        return 0
    try:
        h, w = int(image.shape[0]), int(image.shape[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0
    min_side = min(h, w) * float(min_face_frac)
    return sum(
        1
        for face in detect_faces(image, score_threshold=score_threshold)
        if min(face.bbox[2], face.bbox[3]) >= min_side
    )


def max_faces_in_frame_paths(
    paths: Sequence[str],
    *,
    score_threshold: float = _DEFAULT_SCORE,
    min_face_frac: float = _DEFAULT_MIN_FACE_FRAC,
) -> int:
    """Max single-frame face count over a set of frame image paths; any frame with
    >=2 means that window is multi-face."""
    try:
        import cv2  # type: ignore
    except Exception:  # pragma: no cover
        return 0
    mx = 0
    for p in paths or []:
        img = cv2.imread(str(p))
        if img is None:
            continue
        mx = max(
            mx,
            count_faces_in_image(
                img, score_threshold=score_threshold, min_face_frac=min_face_frac
            ),
        )
    return mx
