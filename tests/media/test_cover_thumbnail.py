"""The WebP cover thumbnail is what the list cards download instead of the cover (issue #206)."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from packages.media.cover_image import (
    THUMBNAIL_MAX_BYTES,
    THUMBNAIL_MAX_LONG_EDGE,
    build_cover_thumbnail_bytes,
)


def _cover_png(width: int = 1080, height: int = 1920) -> bytes:
    """A cover-shaped PNG with real structure (a flat fill would compress to nothing
    and would not exercise the quality ladder)."""
    rng = np.random.default_rng(206)
    gradient = np.linspace(0, 255, width, dtype=np.uint8)
    image = np.repeat(gradient[None, :], height, axis=0)
    image = np.stack([image, image[:, ::-1], np.full_like(image, 128)], axis=-1)
    noise = rng.integers(0, 40, size=image.shape, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", np.clip(image.astype(np.int16) + noise, 0, 255).astype(np.uint8))
    assert ok
    return buf.tobytes()


def test_thumbnail_is_a_webp_within_the_size_and_edge_budget():
    source = _cover_png()
    assert len(source) > 1_000_000  # the cover really is multi-megabyte

    thumbnail = build_cover_thumbnail_bytes(source)

    assert thumbnail[:4] == b"RIFF" and thumbnail[8:12] == b"WEBP"
    assert len(thumbnail) <= THUMBNAIL_MAX_BYTES
    decoded = cv2.imdecode(np.frombuffer(thumbnail, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert max(decoded.shape[:2]) == THUMBNAIL_MAX_LONG_EDGE
    # The whole point: two orders of magnitude smaller than what the card used to pull.
    assert len(thumbnail) * 20 < len(source)


def test_thumbnail_preserves_aspect_ratio():
    thumbnail = build_cover_thumbnail_bytes(_cover_png(1080, 1920))
    decoded = cv2.imdecode(np.frombuffer(thumbnail, dtype=np.uint8), cv2.IMREAD_COLOR)
    height, width = decoded.shape[:2]
    assert height == THUMBNAIL_MAX_LONG_EDGE
    assert width == round(1080 / 1920 * THUMBNAIL_MAX_LONG_EDGE)


def test_thumbnail_encoding_is_deterministic():
    # Selection/rendering in this repo must be reproducible; a thumbnail that
    # re-encodes differently would churn sha256 and break content addressing.
    source = _cover_png()
    assert build_cover_thumbnail_bytes(source) == build_cover_thumbnail_bytes(source)


def test_small_source_is_not_upscaled():
    thumbnail = build_cover_thumbnail_bytes(_cover_png(120, 200))
    decoded = cv2.imdecode(np.frombuffer(thumbnail, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (200, 120)


def test_lowering_the_byte_budget_walks_the_quality_ladder_down():
    source = _cover_png()
    generous = build_cover_thumbnail_bytes(source, max_bytes=THUMBNAIL_MAX_BYTES)
    # Budget deliberately below what the top rung produces, so the ladder must descend.
    tight = build_cover_thumbnail_bytes(source, max_bytes=len(generous) - 1)
    assert len(tight) < len(generous)


def test_an_unreachable_byte_budget_returns_the_smallest_encode_rather_than_failing():
    # An oversized thumbnail is still ~50x smaller than the cover; failing here
    # would take down an export whose video is already produced and paid for.
    source = _cover_png()
    thumbnail = build_cover_thumbnail_bytes(source, max_bytes=1)
    assert thumbnail[:4] == b"RIFF"
    assert len(thumbnail) < len(build_cover_thumbnail_bytes(source))


def test_undecodable_content_raises_rather_than_writing_garbage():
    with pytest.raises(ValueError):
        build_cover_thumbnail_bytes(b"not an image")
    with pytest.raises(ValueError):
        build_cover_thumbnail_bytes(b"")
