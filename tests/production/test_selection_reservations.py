"""Selection Ledger reservation lifecycle: reserve -> commit -> release/expire + TTL.

Spec §6.6 / §17 / §32.10. Concurrent same-case runs must not silently collide on the
same asset: planning reserves a TTL lease, the per-medium production node commits the
shipped asset, cancel/failure releases the rest, and an elapsed TTL expires it.
"""

from __future__ import annotations

from datetime import timedelta

from packages.core.contracts import SelectionReservationRecord, utcnow
from packages.core.storage.repository import Repository


def test_reserve_creates_active_lease_then_blocks_other_run() -> None:
    repo = Repository()
    owned = repo.reserve_selections(
        case_id="case_demo", run_id="run_a", medium="portrait", asset_ids=["asset_portrait_demo"]
    )
    assert len(owned) == 1
    assert owned[0].status == "reserved"
    # A different run sees the slot as held and is skipped (no double reservation).
    other = repo.reserve_selections(
        case_id="case_demo", run_id="run_b", medium="portrait", asset_ids=["asset_portrait_demo"]
    )
    assert other == []
    active = repo.active_selection_reservations(case_id="case_demo", medium="portrait")
    assert {r.run_id for r in active} == {"run_a"}


def test_reserve_is_idempotent_for_same_run() -> None:
    repo = Repository()
    first = repo.reserve_selections(
        case_id="case_demo", run_id="run_a", medium="bgm", asset_ids=["asset_bgm_demo"]
    )
    second = repo.reserve_selections(
        case_id="case_demo", run_id="run_a", medium="bgm", asset_ids=["asset_bgm_demo"]
    )
    assert [r.id for r in first] == [r.id for r in second]
    assert len(repo.selection_reservations) == 1


def test_commit_then_release_keeps_committed_holds() -> None:
    repo = Repository()
    repo.reserve_selections(
        case_id="case_demo",
        run_id="run_a",
        medium="portrait",
        asset_ids=["asset_portrait_demo", "asset_portrait_alt"],
    )
    committed = repo.commit_selection_reservation(
        run_id="run_a", medium="portrait", asset_id="asset_portrait_demo"
    )
    assert committed is not None and committed.status == "committed"
    # Failure/finalize releases only the uncommitted shortlist member.
    released = repo.release_run_reservations(run_id="run_a", only_uncommitted=True)
    assert [r.asset_id for r in released] == ["asset_portrait_alt"]
    # The committed pick still blocks a sibling run (a hard diversity hold).
    active = repo.active_selection_reservations(case_id="case_demo", medium="portrait")
    assert [r.asset_id for r in active] == ["asset_portrait_demo"]
    other = repo.reserve_selections(
        case_id="case_demo", run_id="run_b", medium="portrait", asset_ids=["asset_portrait_demo"]
    )
    assert other == []


def test_commit_returns_none_when_no_live_reservation() -> None:
    repo = Repository()
    assert (
        repo.commit_selection_reservation(run_id="run_x", medium="portrait", asset_id="nope")
        is None
    )


def test_expired_reservation_no_longer_blocks_and_is_reclaimable() -> None:
    repo = Repository()
    stale = SelectionReservationRecord(
        case_id="case_demo",
        run_id="run_stuck",
        medium="portrait",
        asset_id="asset_portrait_demo",
        expires_at=utcnow() - timedelta(seconds=5),
    )
    repo.selection_reservations[stale.id] = stale
    # Lazy expiry on read: the active scan reclaims it.
    active = repo.active_selection_reservations(case_id="case_demo", medium="portrait")
    assert active == []
    assert repo.selection_reservations[stale.id].status == "expired"
    # And a fresh run can now claim the slot.
    owned = repo.reserve_selections(
        case_id="case_demo", run_id="run_new", medium="portrait", asset_ids=["asset_portrait_demo"]
    )
    assert len(owned) == 1 and owned[0].run_id == "run_new"


def test_expire_sweep_marks_stale_reserved_only() -> None:
    repo = Repository()
    repo.reserve_selections(
        case_id="case_demo", run_id="run_live", medium="font", asset_ids=["asset_font_demo"]
    )
    stale = SelectionReservationRecord(
        case_id="case_demo",
        run_id="run_stuck",
        medium="font",
        asset_id="asset_font_demo",
        expires_at=utcnow() - timedelta(seconds=1),
    )
    repo.selection_reservations[stale.id] = stale
    swept = repo.expire_stale_selection_reservations()
    assert [r.run_id for r in swept] == ["run_stuck"]
    # The live (future-TTL) reservation is untouched.
    live = [r for r in repo.selection_reservations.values() if r.run_id == "run_live"]
    assert live and live[0].status == "reserved"


def test_release_can_drop_committed_for_ops_cleanup() -> None:
    repo = Repository()
    repo.reserve_selections(
        case_id="case_demo", run_id="run_a", medium="bgm", asset_ids=["asset_bgm_demo"]
    )
    repo.commit_selection_reservation(run_id="run_a", medium="bgm", asset_id="asset_bgm_demo")
    released = repo.release_run_reservations(run_id="run_a", only_uncommitted=False)
    assert [r.asset_id for r in released] == ["asset_bgm_demo"]
    assert repo.active_selection_reservations(case_id="case_demo", medium="bgm") == []
