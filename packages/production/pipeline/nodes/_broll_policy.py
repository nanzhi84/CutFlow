"""Shared b-roll selection policy helpers.

Single source of truth for the ``include_generic_coverage`` decision so the
three call sites (MaterialPackPlanning, BrollPlanning, BrollCoveragePlanning)
can never drift apart — a past drift between two of these gates is exactly what
silently emptied the b-roll pool.
"""

from __future__ import annotations

from collections.abc import Mapping

from packages.core.contracts import DigitalHumanVideoRequest


def broll_recency_penalties(
    material_payload: Mapping,
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float]]:
    """Per-(asset, clip) and per-(asset, diversity) recency penalties from the pack.

    MaterialPackPlanning is the single node that reads the selection ledger; it stamps
    each b-roll candidate's recency penalty (and its diversity cluster) onto the
    material pack. The b-roll planning nodes read these here instead of re-querying the
    ledger, then hand them to ``demote_recent_broll_candidates`` to re-apply the
    demotion to the narration-ranked pool. Returns ``(by_clip, by_diversity)``.
    """
    by_clip: dict[tuple[str, str], float] = {}
    by_diversity: dict[tuple[str, str], float] = {}
    for item in material_payload.get("broll_candidates", []) or []:
        if not isinstance(item, dict):
            continue
        asset_id = item.get("asset_id")
        if not asset_id:
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        try:
            penalty = max(0.0, float(meta.get("recency_penalty") or 0.0))
        except (TypeError, ValueError):
            penalty = 0.0
        clip_id = str(meta.get("clip_id") or "")
        diversity_key = str(meta.get("diversity_key") or "")
        if clip_id:
            by_clip[(asset_id, clip_id)] = penalty
        by_diversity[(asset_id, diversity_key)] = penalty
    return by_clip, by_diversity


def broll_generic_coverage_enabled(request: DigitalHumanVideoRequest) -> bool:
    """Whether person-free clean clips with no keyword overlap may fill b-roll.

    ``broll_only_v1`` forces it on (its whole purpose is full b-roll coverage);
    every other template follows ``BrollOptions.allow_generic_coverage`` (default
    on). The person/lip-sync gates and the keyword floor for *matched* clips are
    unaffected — this only governs whether the no-overlap fallback is offered.
    """
    return (
        request.workflow_template_id == "broll_only_v1"
        or request.broll.allow_generic_coverage
    )
