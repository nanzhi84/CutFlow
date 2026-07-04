"""Material planning domain: real candidate ranking + b-roll insertion planning.

Pure deterministic functions that take ``AnnotationV4`` clips + narration units
(+ the selection ledger for recency) and produce real material packs and b-roll
insertion plans. No IO, no randomness, no fabricated picks — when annotations /
material are absent the caller soft-degrades.
"""

from packages.planning.material._avoid import avoid_intervals, subtract_bad_spans
from packages.planning.material.broll_pack import (
    BrollCandidate,
    clip_shows_person,
    demote_recent_broll_candidates,
    rank_broll_candidates,
)
from packages.planning.material.broll_plan import (
    BROLL_GEOMETRY_POLICY,
    BrollGeometryPolicy,
    BrollInsertion,
    align_insertions_to_portrait_cuts,
    place_insertion_safely,
    plan_coverage,
    plan_insertions,
)
from packages.planning.material.keywords import (
    ScriptSegment,
    extract_keywords,
    segment_script,
)
from packages.planning.material.portrait_pack import (
    clip_is_lip_sync_usable,
    rank_portrait_clip_candidates,
    score_simple_candidate,
)
from packages.planning.material.portrait_capacity import (
    can_cover_slots_with_cap,
    minimum_reuse_cap,
)
from packages.planning.material.portrait_source import longest_clean_portrait_source_span
from packages.planning.material.shortlist import shortlist_for_windows

__all__ = [
    "avoid_intervals",
    "subtract_bad_spans",
    "BrollCandidate",
    "rank_broll_candidates",
    "demote_recent_broll_candidates",
    "clip_shows_person",
    "plan_coverage",
    "plan_insertions",
    "BROLL_GEOMETRY_POLICY",
    "BrollGeometryPolicy",
    "BrollInsertion",
    "align_insertions_to_portrait_cuts",
    "place_insertion_safely",
    "ScriptSegment",
    "extract_keywords",
    "segment_script",
    "clip_is_lip_sync_usable",
    "rank_portrait_clip_candidates",
    "score_simple_candidate",
    "can_cover_slots_with_cap",
    "minimum_reuse_cap",
    "longest_clean_portrait_source_span",
    "shortlist_for_windows",
]
