"""Cover-image normalization helpers."""

from __future__ import annotations

COVER_TARGET_WIDTH = 1080
COVER_TARGET_HEIGHT = 1920

# List cards never need the full 1080x1920 cover. A cover is a LOSSLESS PNG of
# roughly 2.3 MB; a 512px WebP of the same frame is ~30 KB, so serving the cover
# to a grid of cards was ~19 x 2.3 MB per page load (issue #206).
THUMBNAIL_MAX_LONG_EDGE = 512
THUMBNAIL_MAX_BYTES = 50 * 1024
THUMBNAIL_CONTENT_TYPE = "image/webp"
THUMBNAIL_SUFFIX = ".webp"
# ``thumbnail_label`` of the small WebP a library card loads, as opposed to the
# "first"/"mid" full-resolution frame grabs kept as cover source material.
WEB_THUMBNAIL_LABEL = "web"
# Descending WebP quality ladder, walked until the encode fits the byte budget.
# Capped at 100: OpenCV treats IMWRITE_WEBP_QUALITY > 100 as LOSSLESS, which would
# silently produce a large file and reintroduce the very bug this fixes.
_THUMBNAIL_QUALITY_LADDER = (80, 65, 50, 40, 30)


def build_cover_thumbnail_bytes(
    content: bytes,
    *,
    max_long_edge: int = THUMBNAIL_MAX_LONG_EDGE,
    max_bytes: int = THUMBNAIL_MAX_BYTES,
) -> bytes:
    """Encode a small WebP thumbnail of an image payload, for list/grid cards.

    Deterministic: the same input always yields the same bytes (the quality ladder
    is walked in a fixed order and the first fitting encode wins). Returns the
    smallest encode attempted if even the lowest quality overshoots ``max_bytes``,
    rather than failing — an oversized thumbnail is still orders of magnitude
    smaller than the original cover.
    """
    if not content:
        raise ValueError("Cover image content is empty.")
    cv2, np = _cv2()

    encoded = np.frombuffer(content, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("Cover image content is not decodable.")
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        raise ValueError("Cover image has invalid dimensions.")

    long_edge = max(height, width)
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        target = (max(1, round(width * scale)), max(1, round(height * scale)))
        image = cv2.resize(image, target, interpolation=cv2.INTER_AREA)

    smallest: bytes | None = None
    for quality in _THUMBNAIL_QUALITY_LADDER:
        ok, output = cv2.imencode(THUMBNAIL_SUFFIX, image, [cv2.IMWRITE_WEBP_QUALITY, quality])
        if not ok:
            raise ValueError("Cover thumbnail encoding failed.")
        payload = output.tobytes()
        if len(payload) <= max_bytes:
            return payload
        if smallest is None or len(payload) < len(smallest):
            smallest = payload
    assert smallest is not None  # the ladder is non-empty
    return smallest


def _cv2():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency is required in normal envs
        raise ValueError("OpenCV/numpy are required to normalize cover images.") from exc
    return cv2, np


def normalize_cover_image_bytes(
    content: bytes,
    *,
    target_width: int = COVER_TARGET_WIDTH,
    target_height: int = COVER_TARGET_HEIGHT,
) -> bytes:
    """Center-crop and resize a decoded image payload to the vertical 9:16 cover size."""
    if not content:
        raise ValueError("Cover image content is empty.")
    cv2, np = _cv2()

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
