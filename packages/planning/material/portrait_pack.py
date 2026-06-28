"""Real portrait-clip / bgm / font candidate scoring (replaces the score=1 seed).

Portrait candidates are clip-level talking-head windows, scored on how well the
clip can cover the narration, VLM confidence, and a recency demotion so a source
used in the last run is demoted below a fresh one. bgm/font score on availability
and recency. All pure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from packages.core.contracts import AnnotationV4, SelectionLedgerEntry
from packages.planning.material.subject_terms import PERSON_SUBJECT_TERMS
from packages.planning.selection.recency import RecencyConfig, recency_penalty_for

_COVERAGE_WEIGHT = 60.0
_LIPSYNC_WEIGHT = 30.0
_BASE_AVAILABLE = 10.0
_RECENCY_WEIGHT = 12.0

# A lip-sync source window shorter than this is too small to anchor a narration
# chunk and would only fragment the portrait track, so it is never offered.
_MIN_LIPSYNC_CLIP_SEC = 0.6

@dataclass(frozen=True)
class SimpleCandidate:
    asset_id: str
    score: float
    base_score: float
    recency_penalty: float
    reason: str


def _coverage_ratio(source_duration: float, required_duration: float) -> float:
    if required_duration <= 0:
        return 1.0
    return min(1.0, max(0.0, source_duration) / required_duration)


@dataclass(frozen=True)
class PortraitClipCandidate:
    """One lip-sync-usable clip window inside a unified visual asset."""

    asset_id: str
    clip_id: str
    score: float
    base_score: float
    recency_penalty: float
    source_start: float
    source_end: float
    duration: float
    confidence: float
    reason: str


def clip_is_lip_sync_usable(clip) -> bool:
    """Whether one ``ClipV4`` can serve as a lip-sync source window."""
    usage = clip.usage
    if usage.role.value == "avoid":
        return False
    if usage.voiceover_only:
        return False
    fcm = clip.semantics.face_count_max
    if fcm is not None and fcm > 1:
        return False
    if (float(clip.end) - float(clip.start)) < _MIN_LIPSYNC_CLIP_SEC:
        return False
    if usage.recommended_for_lip_sync:
        return True
    return _looks_like_static_lipsync_source(clip)


def _looks_like_static_lipsync_source(clip) -> bool:
    sem = clip.semantics
    subject = (sem.subject_type or "").lower()
    if not any(term in subject for term in PERSON_SUBJECT_TERMS):
        return False
    if sem.contains_face is False and sem.face_count_max == 0:
        return False
    orientation = (sem.body_orientation or "").lower()
    return (
        sem.mouth_visible is True
        or sem.gaze_to_camera is True
        or "frontal" in orientation
        or "camera" in orientation
    )


def rank_portrait_clip_candidates(
    *,
    annotations: dict[str, AnnotationV4],
    required_duration: float,
    ledger_entries: Sequence[SelectionLedgerEntry] = (),
    recency_cfg: RecencyConfig | None = None,
) -> list[PortraitClipCandidate]:
    """Rank lip-sync-usable clips across (unified ``video``) assets.

    ``annotations`` maps asset_id -> AnnotationV4. Each usable clip scores on how
    much of the required audio its span can cover + the VLM confidence, demoted by
    a recency penalty on its source asset. Empty when no clip clears the gate (the
    honest "no usable portrait" signal — the node then soft-degrades).
    """
    candidates: list[PortraitClipCandidate] = []
    for asset_id, annotation in annotations.items():
        for clip in annotation.clips:
            if not clip_is_lip_sync_usable(clip):
                continue
            duration = max(0.0, float(clip.end) - float(clip.start))
            coverage = _coverage_ratio(duration, required_duration)
            base = (
                _BASE_AVAILABLE
                + coverage * _COVERAGE_WEIGHT
                + float(clip.confidence) * _LIPSYNC_WEIGHT
            )
            penalty = recency_penalty_for(ledger_entries, asset_id=asset_id, cfg=recency_cfg)
            final = max(0.0, base - penalty * _RECENCY_WEIGHT)
            reason = f"lip-sync clip {duration:.1f}s, confidence {float(clip.confidence):.0%}"
            if penalty > 0:
                reason += "; recently used (demoted)"
            candidates.append(
                PortraitClipCandidate(
                    asset_id=asset_id,
                    clip_id=clip.segment_id,
                    score=round(final, 3),
                    base_score=round(base, 3),
                    recency_penalty=round(penalty, 3),
                    source_start=round(float(clip.start), 3),
                    source_end=round(float(clip.end), 3),
                    duration=round(duration, 3),
                    confidence=float(clip.confidence),
                    reason=reason,
                )
            )
    # Longer usable windows win ties (more coverage capacity for the boundary planner).
    candidates.sort(key=lambda c: (-c.score, -c.duration, c.asset_id, c.clip_id))
    return candidates


def score_simple_candidate(
    *,
    asset_id: str,
    medium_label: str,
    ledger_entries: Sequence[SelectionLedgerEntry] = (),
    recency_cfg: RecencyConfig | None = None,
) -> SimpleCandidate:
    """Score an available bgm/font asset (availability base - recency demotion)."""
    base = _BASE_AVAILABLE + _COVERAGE_WEIGHT  # fixed availability score
    penalty = recency_penalty_for(ledger_entries, asset_id=asset_id, cfg=recency_cfg)
    final = max(0.0, base - penalty * _RECENCY_WEIGHT)
    reason = f"available {medium_label}"
    if penalty > 0:
        reason += "; recently used (demoted)"
    return SimpleCandidate(
        asset_id=asset_id,
        score=round(final, 3),
        base_score=round(base, 3),
        recency_penalty=round(penalty, 3),
        reason=reason,
    )
