"""Media-domain annotation package: pure CV sensors + the gated VLM brain.

Two halves, one package:

- the deterministic, key-free sensor suite (step 2a): shot-cut detection,
  voice-activity detection, picture-quality (black/freeze/blur), motion-guard
  (shake/camera-drop), face counting, frame extraction, clip-boundary defences,
  window planning, and the deterministic whole-clip quality report. No network,
  no keys - pure local CV/DSP.
- the VLM annotation layer + assembler: builds the V4 window prompt, parses the
  VLM JSON into ``ClipV4`` (with the V4 error taxonomy + classified retries), and
  assembles a full ``AnnotationV4`` (sensors + windows + per-window VLM semantics +
  quality report). The paid VLM call is GATED behind a real ``vlm.annotation``
  profile + active secret in :mod:`runner`; without one, annotation degrades to a
  sensor-only ``vlm_unconfigured`` result (never fabricated semantics).

The artifact shapes (AnnotationV4 / ClipV4 / windows / quality report) live in
``packages.core.contracts``.
"""

from __future__ import annotations

from .bgm import (
    BGM_TEMPO_BUCKETS,
    FEATURES_UNAVAILABLE,
    LLM_UNCONFIGURED,
    BgmAnnotationResult,
    annotate_bgm,
    extract_audio_features,
    measure_loudness_lufs,
    resolve_audio_profile,
)
from .boundary import apply_safety_inset, has_internal_cut, snap_to_cuts
from .errors import (
    AnnotationV4Error,
    RuntimeVLMError,
    SchemaError,
    SemanticError,
    UnrecoverableError,
)
from .pipeline import V4Config, V4Deps, WindowFailed, run_annotation_v4
from .reclip import (
    DEFAULT_DURATION_DRIFT_THRESHOLD,
    reclip_canonical_to_duration,
    reclipped_or_validated,
)
from .report import build_quality_report, merged_event_duration
from .runner import (
    VLM_UNCONFIGURED,
    GatedAnnotationResult,
    SensorDeps,
    annotate_asset,
    resolve_vlm_profile,
)
from .sensors import (
    count_faces_in_image,
    detect_cv_quality_events,
    detect_shot_cuts,
    detect_speech_islands,
    extract_frame_at_time,
    extract_frames_for_times,
    max_faces_in_frame_paths,
    merge_blur_segments,
    merge_speech_probabilities,
    parse_blackdetect,
    parse_freezedetect,
    reset_detector_cache,
)
from .vlm import build_window_prompt, parse_window_response
from .windows import plan_windows

__all__ = [
    # sensors
    "detect_shot_cuts",
    "detect_speech_islands",
    "merge_speech_probabilities",
    "detect_cv_quality_events",
    "parse_blackdetect",
    "parse_freezedetect",
    "merge_blur_segments",
    "count_faces_in_image",
    "max_faces_in_frame_paths",
    "reset_detector_cache",
    "extract_frame_at_time",
    "extract_frames_for_times",
    # boundary / windows / report
    "snap_to_cuts",
    "apply_safety_inset",
    "has_internal_cut",
    "plan_windows",
    "build_quality_report",
    "merged_event_duration",
    # vlm layer + pipeline
    "build_window_prompt",
    "parse_window_response",
    "run_annotation_v4",
    "V4Config",
    "V4Deps",
    "WindowFailed",
    # replace-source re-clip
    "reclip_canonical_to_duration",
    "reclipped_or_validated",
    "DEFAULT_DURATION_DRIFT_THRESHOLD",
    # gated runner
    "annotate_asset",
    "resolve_vlm_profile",
    "GatedAnnotationResult",
    "SensorDeps",
    "VLM_UNCONFIGURED",
    # bgm / audio annotation
    "annotate_bgm",
    "resolve_audio_profile",
    "extract_audio_features",
    "measure_loudness_lufs",
    "BgmAnnotationResult",
    "LLM_UNCONFIGURED",
    "FEATURES_UNAVAILABLE",
    "BGM_TEMPO_BUCKETS",
    # errors
    "AnnotationV4Error",
    "SchemaError",
    "SemanticError",
    "RuntimeVLMError",
    "UnrecoverableError",
]
