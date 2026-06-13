"""Editing-agent planning domain: the PURE deterministic boundary/timeline planner.

Ported faithfully from digital-human-Cutagent's editing_agent (the planner half).
Given narration units (+ optional pre-detected audio pauses) + portrait source-window
candidates + constraints, it produces a frame-quantized portrait boundary/timeline
plan via: a single-source-of-truth 30fps frame grid (frame_grid), a narration
splitter (narration), semantic+audio-pause boundary assembly (boundary), boundary-
locked chunking + inventory-aware capacity variants (chunks), a fixed-width beam
search (beam) with capacity scoring (packing) and a backtracking feasibility rescue
(rescue).

Audio-pause DETECTION (ffmpeg / silencedetect) is out of scope (step 2b): pauses are
an OPTIONAL input here; with none supplied the planner falls back to semantic-only
boundaries. No IO, no provider calls, no randomness — pure CPU frame/beam math.
"""

from packages.planning.editing.frame_grid import (
    GRID_EPSILON,
    TIMELINE_FPS,
    FrameWindow,
    frame_count,
    frame_index,
    is_on_grid,
    quantize_boundary,
    slice_source_window,
    slice_windows,
    to_frame,
    to_seconds,
)
from packages.planning.editing.narration import (
    SpokenSegment,
    build_narration_units,
    build_narration_units_from_asr,
    build_narration_units_from_script_sentences,
    build_narration_units_without_asr,
    should_prefer_asr_narration_units,
)
from packages.planning.editing.boundary import build_semantic_audio_boundary_entries
from packages.planning.editing.chunks import (
    boundary_chunk_variants,
    build_boundary_locked_chunks,
    derive_capacity_cap_durations,
)
from packages.planning.editing.packing import build_boundary_locked_portrait_plan
from packages.planning.editing.plan import (
    BoundaryConstraints,
    BoundaryTimelinePlan,
    PlannedSegment,
    plan_boundary_timeline,
)

__all__ = [
    # frame grid (single source of truth)
    "TIMELINE_FPS",
    "GRID_EPSILON",
    "FrameWindow",
    "frame_index",
    "to_frame",
    "to_seconds",
    "quantize_boundary",
    "frame_count",
    "is_on_grid",
    "slice_windows",
    "slice_source_window",
    # narration splitter
    "SpokenSegment",
    "build_narration_units",
    "build_narration_units_from_asr",
    "build_narration_units_from_script_sentences",
    "build_narration_units_without_asr",
    "should_prefer_asr_narration_units",
    # boundary assembly + chunking
    "build_semantic_audio_boundary_entries",
    "build_boundary_locked_chunks",
    "boundary_chunk_variants",
    "derive_capacity_cap_durations",
    # capacity packing
    "build_boundary_locked_portrait_plan",
    # top-level pure planner
    "BoundaryConstraints",
    "BoundaryTimelinePlan",
    "PlannedSegment",
    "plan_boundary_timeline",
]
