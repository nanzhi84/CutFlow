"""Cover-image normalization helpers."""

from __future__ import annotations

COVER_TARGET_WIDTH = 1080
COVER_TARGET_HEIGHT = 1920


def normalize_cover_image_bytes(
    content: bytes,
    *,
    target_width: int = COVER_TARGET_WIDTH,
    target_height: int = COVER_TARGET_HEIGHT,
) -> bytes:
    """Center-crop and resize a decoded image payload to the vertical 9:16 cover size."""
    if not content:
        raise ValueError("Cover image content is empty.")
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency is required in normal envs
        raise ValueError("OpenCV/numpy are required to normalize cover images.") from exc

    encoded = np.frombuffer(content, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if image is None or image.size == 0:
        raise ValueError("Cover image content is not decodable.")
    cropped = _center_crop_to_aspect(image, target_width / target_height)
    interpolation = cv2.INTER_AREA if cropped.shape[1] >= target_width else cv2.INTER_CUBIC
    resized = cv2.resize(cropped, (target_width, target_height), interpolation=interpolation)
    ok, output = cv2.imencode(".png", resized)
    if not ok:
        raise ValueError("Cover image normalization failed.")
    return output.tobytes()


def _center_crop_to_aspect(image, target_aspect: float):
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        raise ValueError("Cover image has invalid dimensions.")
    current_aspect = width / height
    if abs(current_aspect - target_aspect) <= 0.001:
        return image
    if current_aspect > target_aspect:
        crop_width = max(1, round(height * target_aspect))
        x0 = max(0, (width - crop_width) // 2)
        return image[:, x0 : x0 + crop_width]
    crop_height = max(1, round(width / target_aspect))
    y0 = max(0, (height - crop_height) // 2)
    return image[y0 : y0 + crop_height, :]
