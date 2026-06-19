"""Usage-aware recency demotion sourced from the case selection ledger.

Ported from the origin ``case_selection.recency.compute_recency``: turns a
case's recent ``SelectionLedgerEntry`` history into a normalized recency penalty
so a clip picked in an earlier run is demoted below a fresh clip on the next run
(diversity is a soft penalty, never a hard filter). Pure: callers pass the
already-queried ledger entries (most-recent-first) so this stays IO-free.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from packages.core.contracts import SelectionLedgerEntry


@dataclass(frozen=True)
class RecencyConfig:
    decay: float = 0.75
    max_penalty: float = 0.7
    window: int = 12
    cluster_factor: float = 0.5


@dataclass(frozen=True)
class RecencyResult:
    usage_count: int
    penalty: float


def compute_recency(
    entries: Sequence[SelectionLedgerEntry],
    *,
    asset_id: str,
    clip_id: str | None = None,
    diversity_key: str | None = None,
    cfg: RecencyConfig | None = None,
) -> RecencyResult:
    """Normalized recency penalty for one candidate.

    ``entries`` must be ordered most-recent-first; only the first
    ``cfg.window`` are considered. Without ``clip_id`` a same-asset hit weighs
    full. With ``clip_id`` an exact asset+clip hit weighs full, while another
    clip from the same asset is treated as a lighter cluster hit. A same
    diversity-cluster hit (no asset match) also weighs ``cluster_factor``. Each
    hit decays by ``decay ** position`` so older usage matters less. The penalty
    is clamped to ``[0, max_penalty]``.
    """
    config = cfg or RecencyConfig()
    windowed = list(entries)[: config.window]

    key = str(asset_id or "").strip()
    clip_key = str(clip_id or "").strip()
    cluster = str(diversity_key or "").strip()

    penalty = 0.0
    usage_count = 0
    for pos, entry in enumerate(windowed):
        same_asset = bool(key) and entry.asset_id == key
        same_clip = same_asset and bool(clip_key) and entry.clip_id == clip_key
        same_asset_unscoped = same_asset and not clip_key
        same_asset_other_clip = same_asset and bool(clip_key) and not same_clip
        same_cluster = bool(cluster) and (entry.diversity_key or "") == cluster
        if not (same_clip or same_asset_unscoped or same_asset_other_clip or same_cluster):
            continue
        usage_count += 1
        factor = 1.0 if same_clip or same_asset_unscoped else config.cluster_factor
        penalty += (config.decay**pos) * factor

    penalty = min(config.max_penalty, penalty)
    return RecencyResult(usage_count=usage_count, penalty=round(penalty, 6))


def recency_penalty_for(
    entries: Sequence[SelectionLedgerEntry],
    *,
    asset_id: str,
    clip_id: str | None = None,
    diversity_key: str | None = None,
    cfg: RecencyConfig | None = None,
) -> float:
    """Convenience: just the clamped penalty for one candidate."""
    return compute_recency(
        entries, asset_id=asset_id, clip_id=clip_id, diversity_key=diversity_key, cfg=cfg
    ).penalty
