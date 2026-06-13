"""FinalizeRunReport node: emit run reports, record selections, GC ephemerals."""

from __future__ import annotations

import logging

from packages.core.contracts import ArtifactKind
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._selection import selection_entries_from_state

logger = logging.getLogger(__name__)

EPHEMERAL_ARTIFACT_KINDS = {
    ArtifactKind.video_portrait_track,
    ArtifactKind.video_lipsync,
    ArtifactKind.video_rendered,
}


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    public_artifact, debug_artifact = ctx.write_report(failed=False)
    try:
        ctx.repository.record_selection_ledger_entries(selection_entries_from_state(ctx.run, state))
    except Exception:
        logger.warning("Failed to record selection ledger for run %s.", ctx.run.id, exc_info=True)
    for artifact in state.artifacts.values():
        if artifact.kind not in EPHEMERAL_ARTIFACT_KINDS or not artifact.uri:
            continue
        try:
            ctx.object_store().delete(artifact.uri)
        except Exception:
            logger.warning(
                "Failed to delete ephemeral artifact %s at %s.",
                artifact.id,
                artifact.uri,
                exc_info=True,
            )
    return NodeOutput(artifacts=[public_artifact, debug_artifact])
