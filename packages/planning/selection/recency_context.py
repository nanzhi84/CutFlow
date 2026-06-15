"""Per-candidate portrait recency context from the case selection ledger.

Ported from the origin ``material_planning/recency_context.build_portrait_recency_context_from_ledger``
(adapted to the genesis ``SelectionLedgerEntry`` schema). Turns a case's recent
portrait ledger rows into a ``recent_usage`` dict per candidate carrying the
weighted-recency + opening-guard signals the already-ported scoring side consumes
(``_candidate.portrait_recent_usage_penalty`` / ``is_recent_portrait_candidate`` /
``portrait_boundary_option_score``).

Genesis ledger schema mapping vs. the origin's richer rows:
  - ``run_id``    -> task grouping (one video = one run; most-recent-first);
  - ``asset_id``  -> the portrait template identity (``template_id``);
  - ``slot_phase``-> ``"portrait_opening"`` marks the opening segment (opening guard);
  - ``diversity_key`` -> the similarity cluster.

Pure: callers pass the already-queried entries (most-recent-first), so this stays
IO-free and identically replayable by tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from packages.core.contracts import SelectionLedgerEntry

# Tunables ported verbatim from origin material_planning/constants.py so runtime
# scoring matches the origin's calibration.
RECENCY_DECAY = 0.75
PORTRAIT_RECENT_TASK_PENALTY = 0.34
PORTRAIT_RECENT_SEGMENT_PENALTY = 0.055
PORTRAIT_RECENT_OPENING_PENALTY = 0.18
MAX_PORTRAIT_RECENCY_PENALTY = 0.9
MAX_PORTRAIT_SIMILARITY_PENALTY = 0.18
PORTRAIT_SIMILARITY_MATCH_STRENGTH = 0.18

# slot_phase value that marks the first (opening) portrait segment of a run.
PORTRAIT_OPENING_SLOT_PHASE = "portrait_opening"
PORTRAIT_MAIN_SLOT_PHASE = "portrait_main"

_DEFAULT_WINDOW = 12


def _empty_context() -> dict[str, Any]:
    return {
        "is_recently_used": False,
        "recency_penalty": 0.0,
        "exact_recency_penalty": 0.0,
        "similarity_penalty": 0.0,
        "summary": "最近同案例任务暂无历史记录。",
        "history_task_count": 0,
        "history_segment_count": 0,
        "recent_task_use_count": 0,
        "recent_segment_use_count": 0,
        "recent_opening_use_count": 0,
        "similar_recent_task_use_count": 0,
        "similar_recent_segment_use_count": 0,
        "similar_recent_opening_use_count": 0,
    }


def build_portrait_recency_context_from_ledger(
    *,
    entries: Sequence[SelectionLedgerEntry],
    template_id: str,
    diversity_key: str | None = None,
    window: int = _DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Weighted portrait recency for one candidate, sourced from ledger rows.

    Replicates the origin's weighted penalty semantics over the genesis ledger:
    per-task (run) time decay, a strength-weighted per-segment penalty
    (template_id 1.0 / diversity_key 0.18), an opening penalty when a matched
    segment opened a run, a once-per-task task penalty, and a hard cap. ``entries``
    must arrive most-recent-first; only the first ``window`` are considered.
    """
    windowed = [entry for entry in list(entries)[: max(0, window)]]
    if not windowed:
        return _empty_context()

    cand_template_id = str(template_id or "").strip()
    cand_diversity_key = str(diversity_key or "").strip()

    # Group windowed entries by run_id in first-seen (most-recent-first) order so
    # task_index 0 is the most recent video (mirrors the origin's enumerate over a
    # newest-first history). Rows with an empty run_id each form a singleton task.
    task_order: list[str] = []
    tasks_by_id: dict[str, list[SelectionLedgerEntry]] = {}
    for entry in windowed:
        task_key = str(entry.run_id or "") or f"__row_{entry.id}"
        if task_key not in tasks_by_id:
            tasks_by_id[task_key] = []
            task_order.append(task_key)
        tasks_by_id[task_key].append(entry)

    exact_total_penalty = 0.0
    similar_total_penalty = 0.0
    history_segment_count = 0
    exact_segment_count = 0
    similar_segment_count = 0
    exact_opening_count = 0
    similar_opening_count = 0
    exact_task_keys: list[str] = []
    similar_task_keys: list[str] = []

    for task_index, task_key in enumerate(task_order):
        task_decay = RECENCY_DECAY**task_index
        exact_task_penalty_applied = False
        similar_task_penalty_applied = False
        task_has_exact = False
        task_has_similar = False
        for entry in tasks_by_id[task_key]:
            history_segment_count += 1
            entry_template_id = str(entry.asset_id or "").strip()
            entry_diversity_key = str(entry.diversity_key or "").strip()
            match_strength = 0.0
            exact_match = False
            if cand_template_id and entry_template_id == cand_template_id:
                match_strength = 1.0
                exact_match = True
            elif cand_diversity_key and entry_diversity_key == cand_diversity_key:
                match_strength = PORTRAIT_SIMILARITY_MATCH_STRENGTH
            if match_strength <= 0.0:
                continue
            is_opening = str(entry.slot_phase or "").strip().lower() == PORTRAIT_OPENING_SLOT_PHASE
            weighted_penalty = PORTRAIT_RECENT_SEGMENT_PENALTY * match_strength * task_decay
            if is_opening:
                weighted_penalty += PORTRAIT_RECENT_OPENING_PENALTY * match_strength * task_decay
            if exact_match and not exact_task_penalty_applied:
                weighted_penalty += PORTRAIT_RECENT_TASK_PENALTY * match_strength * task_decay
                exact_task_penalty_applied = True
            elif not exact_match and not similar_task_penalty_applied:
                weighted_penalty += PORTRAIT_RECENT_TASK_PENALTY * match_strength * task_decay
                similar_task_penalty_applied = True
            if exact_match:
                exact_total_penalty += weighted_penalty
                exact_segment_count += 1
                task_has_exact = True
                if is_opening:
                    exact_opening_count += 1
            else:
                similar_total_penalty += weighted_penalty
                similar_segment_count += 1
                task_has_similar = True
                if is_opening:
                    similar_opening_count += 1
        if task_has_exact:
            exact_task_keys.append(task_key)
        if task_has_similar:
            similar_task_keys.append(task_key)

    is_recently_used = exact_segment_count > 0
    similarity_penalty = round(min(MAX_PORTRAIT_SIMILARITY_PENALTY, similar_total_penalty), 3)
    recency_penalty = min(MAX_PORTRAIT_RECENCY_PENALTY, exact_total_penalty + similarity_penalty)

    if is_recently_used:
        summary = (
            f"最近 {len(windowed)} 条同案例选择中出现 {exact_segment_count} 次"
            f"（{len(exact_task_keys)} 条视频）。"
        )
    else:
        summary = f"最近 {len(windowed)} 条同案例选择未使用。"

    return {
        "is_recently_used": is_recently_used,
        "recency_penalty": round(recency_penalty, 3),
        "exact_recency_penalty": round(min(MAX_PORTRAIT_RECENCY_PENALTY, exact_total_penalty), 3),
        "similarity_penalty": similarity_penalty,
        "summary": summary,
        "history_task_count": len(task_order),
        "history_segment_count": history_segment_count,
        "recent_task_use_count": len(exact_task_keys),
        "recent_segment_use_count": exact_segment_count,
        "recent_opening_use_count": exact_opening_count,
        "similar_recent_task_use_count": len(similar_task_keys),
        "similar_recent_segment_use_count": similar_segment_count,
        "similar_recent_opening_use_count": similar_opening_count,
    }
