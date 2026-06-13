"""Real portrait / bgm / font candidate scoring (replaces the score=1 seed).

Portrait does not need keyword matching against the script — it is the main
talking-head track — so its real score reflects how well the asset can cover the
narration (source duration vs. required duration), its annotated lip-sync
suitability, and a recency demotion so a portrait used in the last run is
demoted below a fresh one. bgm/font score on availability + recency. All pure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from packages.core.contracts import AnnotationV4, SelectionLedgerEntry
from packages.planning.selection.recency import RecencyConfig, recency_penalty_for

_COVERAGE_WEIGHT = 60.0
_LIPSYNC_WEIGHT = 30.0
_BASE_AVAILABLE = 10.0
_RECENCY_WEIGHT = 12.0


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


def _lipsync_suitability(annotation: AnnotationV4 | None) -> float:
    if annotation is None:
        return 0.5
    raw = annotation.quality_report.get("lip_sync_suitability_score")
    try:
        return min(1.0, max(0.0, float(raw) / 100.0))
    except (TypeError, ValueError):
        return 0.5


def score_portrait_candidate(
    *,
    asset_id: str,
    source_duration: float,
    required_duration: float,
    annotation: AnnotationV4 | None = None,
    ledger_entries: Sequence[SelectionLedgerEntry] = (),
    recency_cfg: RecencyConfig | None = None,
) -> SimpleCandidate:
    """Score one portrait asset on coverage + lip-sync suitability - recency."""
    coverage = _coverage_ratio(source_duration, required_duration)
    lipsync = _lipsync_suitability(annotation)
    base = _BASE_AVAILABLE + coverage * _COVERAGE_WEIGHT + lipsync * _LIPSYNC_WEIGHT
    penalty = recency_penalty_for(ledger_entries, asset_id=asset_id, cfg=recency_cfg)
    final = max(0.0, base - penalty * _RECENCY_WEIGHT)
    reason = f"coverage {coverage:.0%}, lip-sync {lipsync:.0%}"
    if penalty > 0:
        reason += "; recently used (demoted)"
    return SimpleCandidate(
        asset_id=asset_id,
        score=round(final, 3),
        base_score=round(base, 3),
        recency_penalty=round(penalty, 3),
        reason=reason,
    )


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
