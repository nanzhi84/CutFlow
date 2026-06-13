"""Deterministic, key-free CV/VAD/scene-detection sensors for annotation.

Each sensor is small, typed, and fail-open: a missing dependency or unreadable
input yields an empty result rather than raising. The VLM annotation layer (a
later step) consumes these sensor products.
"""

from __future__ import annotations

from .cv_quality import (
    detect_cv_quality_events,
    merge_blur_segments,
    parse_blackdetect,
    parse_freezedetect,
)
from .faces import (
    count_faces_in_image,
    max_faces_in_frame_paths,
    reset_detector_cache,
)
from .frames import extract_frame_at_time, extract_frames_for_times
from .motion import MotionGuard
from .shots import detect_shot_cuts
from .voice_activity import detect_speech_islands, merge_speech_probabilities

__all__ = [
    "detect_cv_quality_events",
    "parse_blackdetect",
    "parse_freezedetect",
    "merge_blur_segments",
    "count_faces_in_image",
    "max_faces_in_frame_paths",
    "reset_detector_cache",
    "extract_frame_at_time",
    "extract_frames_for_times",
    "MotionGuard",
    "detect_shot_cuts",
    "detect_speech_islands",
    "merge_speech_probabilities",
]
