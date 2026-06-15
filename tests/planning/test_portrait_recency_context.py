"""Portrait recency / opening-guard context built from the selection ledger.

These prove the previously-dead recency model now produces real signals the ported
scoring side (``_candidate.portrait_recent_usage_penalty`` /
``is_recent_portrait_candidate`` / the opening penalty) consumes — so a recently-used
template is demoted and a consecutive opening reuse is penalised.
"""

from __future__ import annotations

from datetime import timedelta

from packages.core.contracts import SelectionLedgerEntry, utcnow
from packages.planning.editing import _candidate
from packages.planning.selection.recency_context import (
    PORTRAIT_OPENING_SLOT_PHASE,
    build_portrait_recency_context_from_ledger,
)


def _entry(run_id: str, asset_id: str, slot_phase: str, *, ago_seconds: int = 0) -> SelectionLedgerEntry:
    return SelectionLedgerEntry(
        case_id="case_demo",
        run_id=run_id,
        medium="portrait",
        asset_id=asset_id,
        slot_phase=slot_phase,
        created_at=utcnow() - timedelta(seconds=ago_seconds),
    )


def test_no_history_is_fresh() -> None:
    ctx = build_portrait_recency_context_from_ledger(entries=[], template_id="T1")
    assert ctx["is_recently_used"] is False
    assert ctx["recency_penalty"] == 0.0
    assert ctx["recent_opening_use_count"] == 0
    # And the ported scoring side treats it as a fresh candidate.
    assert _candidate.is_recent_portrait_candidate({"recent_usage": ctx}) is False
    assert _candidate.portrait_recent_usage_penalty({"recent_usage": ctx}) == 0.0


def test_exact_template_match_is_recent_and_demoted() -> None:
    entries = [_entry("run_prev", "T1", "portrait_main")]
    ctx = build_portrait_recency_context_from_ledger(entries=entries, template_id="T1")
    assert ctx["is_recently_used"] is True
    assert ctx["recency_penalty"] > 0
    assert ctx["recent_task_use_count"] == 1
    # The dead-default is now alive: scoring marks this candidate recent + penalises it.
    candidate = {"template_id": "T1", "recent_usage": ctx, "recency_penalty": ctx["recency_penalty"]}
    assert _candidate.is_recent_portrait_candidate(candidate) is True
    assert _candidate.portrait_recent_usage_penalty(candidate) > 0


def test_unmatched_template_stays_fresh() -> None:
    entries = [_entry("run_prev", "T_OTHER", "portrait_main")]
    ctx = build_portrait_recency_context_from_ledger(entries=entries, template_id="T1")
    assert ctx["is_recently_used"] is False
    assert ctx["recency_penalty"] == 0.0


def test_opening_reuse_adds_opening_signal_and_extra_penalty() -> None:
    # Same template opened the previous run -> opening guard signal + a strictly higher
    # penalty than the same template used only mid-body.
    opening = build_portrait_recency_context_from_ledger(
        entries=[_entry("run_prev", "T1", PORTRAIT_OPENING_SLOT_PHASE)],
        template_id="T1",
    )
    mid_body = build_portrait_recency_context_from_ledger(
        entries=[_entry("run_prev", "T1", "portrait_main")],
        template_id="T1",
    )
    assert opening["recent_opening_use_count"] == 1
    assert mid_body["recent_opening_use_count"] == 0
    assert opening["recency_penalty"] > mid_body["recency_penalty"]
    # The opening penalty term (recent_opening_use_count * 10) flows into the score.
    open_pen = _candidate.portrait_recent_usage_penalty({"template_id": "T1", "recent_usage": opening})
    mid_pen = _candidate.portrait_recent_usage_penalty({"template_id": "T1", "recent_usage": mid_body})
    assert open_pen > mid_pen


def test_decay_makes_older_runs_matter_less() -> None:
    recent = build_portrait_recency_context_from_ledger(
        entries=[_entry("run_recent", "T1", "portrait_main", ago_seconds=10)],
        template_id="T1",
    )
    # Same template, but only in an OLDER run (a fresher unrelated run sits in front).
    older = build_portrait_recency_context_from_ledger(
        entries=[
            _entry("run_recent", "T_OTHER", "portrait_main", ago_seconds=10),
            _entry("run_old", "T1", "portrait_main", ago_seconds=1000),
        ],
        template_id="T1",
    )
    assert recent["recency_penalty"] > older["recency_penalty"] > 0


def test_window_limits_considered_history() -> None:
    # T1 used only far back beyond the window -> not counted.
    entries = [_entry(f"run_{i}", "T_OTHER", "portrait_main", ago_seconds=i) for i in range(12)]
    entries.append(_entry("run_ancient", "T1", "portrait_main", ago_seconds=9999))
    ctx = build_portrait_recency_context_from_ledger(entries=entries, template_id="T1", window=12)
    assert ctx["is_recently_used"] is False
