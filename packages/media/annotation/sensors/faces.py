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
from pathlib import Path

logger = logging.getLogger(__name__)

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
    det = _get_detector(score_threshold)
    if det is None:
        return 0
    try:
        h, w = int(image.shape[0]), int(image.shape[1])
        det.setInputSize((w, h))
        _, faces = det.detect(image)
    except Exception as exc:  # pragma: no cover
        logger.debug("[faces] detect failed: %s", exc)
        return 0
    if faces is None:
        return 0
    min_side = min(h, w) * float(min_face_frac)
    return sum(1 for f in faces if min(float(f[2]), float(f[3])) >= min_side)


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
