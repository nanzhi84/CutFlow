from __future__ import annotations

import cv2  # type: ignore
import numpy as np  # type: ignore
import pytest

from packages.media.annotation.sensors import (
    SceneTextDetectionError,
    detect_scene_text_strict,
    scene_text_detector_available,
)
from packages.media.annotation.sensors.scene_text import reset_detector_cache

pytestmark = pytest.mark.skipif(
    not scene_text_detector_available(),
    reason="scene-text DB detector (cv2 + PP-OCRv3 model) unavailable",
)


def _text_image() -> np.ndarray:
    img = np.full((480, 640, 3), 30, dtype=np.uint8)
    cv2.putText(img, "SALE 88", (60, 260), cv2.FONT_HERSHEY_SIMPLEX, 3.2, (245, 245, 245), 8)
    return img


def _stripe_image() -> np.ndarray:
    # Structural horizontal texture (ceiling beams / grilles) that the old
    # morphology heuristic misread as text -- the DB detector must ignore it.
    img = np.full((480, 640, 3), 20, dtype=np.uint8)
    for y in range(0, 480, 20):
        cv2.line(img, (0, y), (640, y), (120, 120, 120), 4)
    return img


def test_detects_synthetic_text_region():
    reset_detector_cache()
    regions = detect_scene_text_strict(_text_image())
    assert len(regions) >= 1
    # The detection covers the drawn glyphs (roughly centered) and is normalized.
    for x, y, w, h in regions:
        assert 0.0 <= x and 0.0 <= y and w > 0.0 and h > 0.0
        assert x + w <= 1.0 and y + h <= 1.0
    assert any(0.2 < y < 0.7 for _x, y, _w, _h in regions)


def test_structural_stripes_are_not_text():
    reset_detector_cache()
    assert detect_scene_text_strict(_stripe_image()) == []


def test_blank_image_has_no_text():
    reset_detector_cache()
    blank = np.full((480, 640, 3), 20, dtype=np.uint8)
    assert detect_scene_text_strict(blank) == []


def test_none_image_raises():
    with pytest.raises(SceneTextDetectionError):
        detect_scene_text_strict(None)
