"""Ephemeral artifact GC helpers for terminal run lifecycle."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from packages.core.contracts import ArtifactKind
from packages.production.pipeline._run_state import RunState

logger = logging.getLogger(__name__)

EPHEMERAL_ARTIFACT_KINDS = {
    ArtifactKind.video_portrait_track,
    ArtifactKind.video_lipsync,
    ArtifactKind.video_rendered,
}


def failed_ephemeral_retention_policy() -> str | None:
    if os.getenv("CUTAGENT_KEEP_FAILED_EPHEMERAL") == "1":
        return "CUTAGENT_KEEP_FAILED_EPHEMERAL"
    retention_hours = os.getenv("CUTAGENT_EPHEMERAL_FAILED_RETENTION_HOURS")
    if retention_hours is None:
        return None
    try:
        return "CUTAGENT_EPHEMERAL_FAILED_RETENTION_HOURS" if float(retention_hours) > 0 else None
    except ValueError:
        return None


def gc_ephemeral_artifacts(object_store, state: RunState, *, run_id: str) -> list[str]:
    deleted_uris: list[str] = []
    for artifact in state.artifacts.values():
        if artifact.kind not in EPHEMERAL_ARTIFACT_KINDS or not artifact.uri:
            continue
        try:
            object_store.delete(artifact.uri)
        except Exception:
            logger.warning(
                "Failed to delete ephemeral artifact %s at %s for run %s.",
                artifact.id,
                artifact.uri,
                run_id,
                exc_info=True,
            )
            continue
        deleted_uris.append(artifact.uri)
    return deleted_uris


def record_ephemeral_gc_event(
    repository,
    *,
    run_id: str,
    terminal_status: str,
    deleted_uris: Sequence[str],
    skipped: bool,
    retention_policy: str | None = None,
) -> None:
    try:
        repository.create_event(
            "workflow.run.ephemeral_gc",
            "run",
            run_id,
            {
                "run_id": run_id,
                "terminal_status": terminal_status,
                "deleted_count": len(deleted_uris),
                "skipped": skipped,
                "retention_policy": retention_policy,
            },
            dedupe_key=f"{run_id}:ephemeral-gc:{terminal_status}",
            payload_schema="EphemeralArtifactGCEvent.v1",
            event_type="artifact_gc",
            run_id=run_id,
            status=terminal_status,
            message="Ephemeral artifact GC completed.",
        )
    except Exception:
        logger.warning("Failed to record ephemeral GC event for run %s.", run_id, exc_info=True)
