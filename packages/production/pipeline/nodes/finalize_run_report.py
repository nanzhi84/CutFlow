"""FinalizeRunReport node: emit run reports, record selections, GC ephemerals."""

from __future__ import annotations

import logging

from packages.core.contracts import RunStatus
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._selection import selection_entries_from_state
from packages.production.pipeline.ephemeral_gc import gc_ephemeral_artifacts, record_ephemeral_gc_event

logger = logging.getLogger(__name__)


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    public_artifact, debug_artifact = ctx.write_report(failed=False)
    entries = selection_entries_from_state(ctx.run, state)
    try:
        ctx.repository.record_selection_ledger_entries(entries)
    except Exception:
        logger.warning("Failed to record selection ledger for run %s.", ctx.run.id, exc_info=True)
    # §6.6 commit -> release: promote the reservation of every asset that actually
    # shipped to ``committed`` (a hard diversity hold), then release this run's
    # remaining uncommitted shortlist leases so a sibling run can claim them. Wrapped
    # so a reservation hiccup never blocks the run report from being written.
    try:
        for entry in entries:
            ctx.repository.commit_selection_reservation(
                run_id=ctx.run.id, medium=entry.medium, asset_id=entry.asset_id
            )
        ctx.repository.release_run_reservations(run_id=ctx.run.id, only_uncommitted=True)
    except Exception:
        logger.warning(
            "Failed to commit/release selection reservations for run %s.", ctx.run.id, exc_info=True
        )
    deleted_uris = gc_ephemeral_artifacts(ctx.object_store(), state, run_id=ctx.run.id)
    record_ephemeral_gc_event(
        ctx.repository,
        run_id=ctx.run.id,
        terminal_status=RunStatus.succeeded.value,
        deleted_uris=deleted_uris,
        skipped=False,
    )
    return NodeOutput(artifacts=[public_artifact, debug_artifact])
