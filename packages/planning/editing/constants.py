"""Editing-agent calibration constants, copied verbatim from the origin.

These thresholds ARE the calibration of the boundary planner — they are not free
parameters to re-derive. Values mirror digital-human-Cutagent's
``EditingAgentSettings`` defaults (settings.editing_agent.*) and the module-level
constants in ``editing_agent/semantic_audio.py`` / ``boundary_planning.py``.
"""

from __future__ import annotations

# --- Audio-pause window matching (used when optional pauses are fed in) ---
AUDIO_PAUSE_SEARCH_BACK = 0.18
AUDIO_PAUSE_SEARCH_FORWARD = 0.22
AUDIO_PAUSE_CUT_OFFSET = 0.02
AUDIO_BOUNDARY_ADVANCE_LIMIT = 0.18
AUDIO_PAUSE_BOUNDARY_EPS = 0.03
AUDIO_PAUSE_STRONG_MIN_DURATION = 0.12

# --- Long-gap / capacity boundary protection ---
BOUNDARY_LONG_GAP_HARD_MAX_DURATION = 12.0
BOUNDARY_LONG_GAP_MIN_SEGMENT = 3.0

# --- Beam search ---
BOUNDARY_BEAM_WIDTH = 256
DEFAULT_BRANCH_FACTOR = 18
ADJACENCY_PENALTY = 6.0

# --- Inventory-aware capacity-cap split variant (prod bc881391) ---
CAPACITY_CAP_MIN_DURATION = 6.0
CAPACITY_CAP_MARGIN = 0.05
CAPACITY_CAP_MIN_GAP = 0.25
CAPACITY_CAP_MAX_COUNT = 3

# --- Backtracking rescue cut-offs ---
RESCUE_DEADLINE_SECONDS = 1.5
RESCUE_NODE_LIMIT = 200_000
