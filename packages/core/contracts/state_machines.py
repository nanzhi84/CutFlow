from __future__ import annotations

from enum import Enum
from typing import Any

from packages.core.contracts import ErrorCode, JobStatus, NodeStatus, ProviderStatus, RunStatus
from packages.core.workflow import NodeExecutionError


State = Enum | str


JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.draft: frozenset({JobStatus.queued, JobStatus.cancelled}),
    JobStatus.queued: frozenset({JobStatus.running, JobStatus.cancelled}),
    JobStatus.running: frozenset({JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}),
    JobStatus.succeeded: frozenset({JobStatus.queued, JobStatus.archived}),
    JobStatus.failed: frozenset({JobStatus.queued, JobStatus.archived}),
    JobStatus.cancelled: frozenset({JobStatus.archived}),
    JobStatus.archived: frozenset(),
}

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.created: frozenset({RunStatus.admitted, RunStatus.cancelled}),
    RunStatus.admitted: frozenset({RunStatus.running, RunStatus.cancelled}),
    RunStatus.running: frozenset(
        {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelling, RunStatus.cancelled}
    ),
    RunStatus.cancelling: frozenset({RunStatus.cancelled}),
    RunStatus.succeeded: frozenset(),
    RunStatus.failed: frozenset(),
    RunStatus.cancelled: frozenset(),
}

NODE_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.pending: frozenset({NodeStatus.running, NodeStatus.skipped}),
    NodeStatus.running: frozenset(
        {NodeStatus.succeeded, NodeStatus.degraded, NodeStatus.failed, NodeStatus.cancelled}
    ),
    NodeStatus.succeeded: frozenset(),
    NodeStatus.skipped: frozenset(),
    NodeStatus.degraded: frozenset(),
    NodeStatus.failed: frozenset(),
    NodeStatus.cancelled: frozenset(),
}

PROVIDER_TRANSITIONS: dict[ProviderStatus, frozenset[ProviderStatus]] = {
    ProviderStatus.prepared: frozenset({ProviderStatus.submitted, ProviderStatus.failed}),
    ProviderStatus.submitted: frozenset(
        {
            ProviderStatus.polling,
            ProviderStatus.succeeded,
            ProviderStatus.failed,
            ProviderStatus.timed_out,
            ProviderStatus.cancelled,
        }
    ),
    ProviderStatus.polling: frozenset(
        {
            ProviderStatus.succeeded,
            ProviderStatus.failed,
            ProviderStatus.timed_out,
            ProviderStatus.cancelled,
        }
    ),
    ProviderStatus.succeeded: frozenset(),
    ProviderStatus.failed: frozenset(),
    ProviderStatus.timed_out: frozenset(),
    ProviderStatus.cancelled: frozenset(),
}

PROMPT_VERSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"reviewing"}),
    "reviewing": frozenset({"approved", "deprecated"}),
    "approved": frozenset({"published", "deprecated"}),
    "published": frozenset({"deprecated", "rolled_back"}),
    "deprecated": frozenset(),
    "rolled_back": frozenset(),
}

CASE_MEMORY_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"approved", "rejected"}),
    "approved": frozenset({"active", "rejected"}),
    "active": frozenset({"deprecated", "superseded"}),
    "deprecated": frozenset(),
    "rejected": frozenset(),
    "superseded": frozenset(),
}

UPLOAD_SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"uploading", "failed", "cancelled", "expired"}),
    "uploading": frozenset({"completed", "failed", "cancelled", "expired"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}

PUBLISH_BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"processing"}),
    "processing": frozenset({"review_ready", "publishing", "partial_failed"}),
    "review_ready": frozenset({"publishing"}),
    "publishing": frozenset({"completed", "partial_failed"}),
    "completed": frozenset(),
    "partial_failed": frozenset({"publishing", "completed"}),
}

PUBLISH_ITEM_TRANSITIONS: dict[str, frozenset[str]] = {
    "uploaded": frozenset({"normalizing", "excluded"}),
    "normalizing": frozenset({"asr_running", "generation_failed", "excluded"}),
    "asr_running": frozenset({"copy_running", "generation_failed", "excluded"}),
    "copy_running": frozenset({"cover_running", "generation_failed", "excluded"}),
    "cover_running": frozenset({"review_ready", "manual_review_ready", "generation_failed", "excluded"}),
    "review_ready": frozenset({"publishing", "excluded"}),
    "manual_review_ready": frozenset({"publishing", "excluded"}),
    "publishing": frozenset({"published", "publish_failed"}),
    "published": frozenset(),
    "generation_failed": frozenset(),
    "publish_failed": frozenset({"publishing", "excluded"}),
    "excluded": frozenset(),
}

PUBLISH_ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"manual_review_ready", "scheduled", "published", "failed"}),
    "manual_review_ready": frozenset({"scheduled", "published", "failed"}),
    "scheduled": frozenset({"published", "failed"}),
    "published": frozenset(),
    "failed": frozenset(),
}

TRANSITIONS: dict[str, dict[Any, frozenset[Any]]] = {
    "job": JOB_TRANSITIONS,
    "run": RUN_TRANSITIONS,
    "node": NODE_TRANSITIONS,
    "provider": PROVIDER_TRANSITIONS,
    "prompt_version": PROMPT_VERSION_TRANSITIONS,
    "case_memory": CASE_MEMORY_TRANSITIONS,
    "upload_session": UPLOAD_SESSION_TRANSITIONS,
    "publish_batch": PUBLISH_BATCH_TRANSITIONS,
    "publish_item": PUBLISH_ITEM_TRANSITIONS,
    "publish_attempt": PUBLISH_ATTEMPT_TRANSITIONS,
}


def _state_value(state: State) -> str:
    return state.value if isinstance(state, Enum) else str(state)


def assert_transition(kind: str, from_state: State, to_state: State) -> None:
    machine = TRANSITIONS[kind]
    if from_state == to_state:
        return
    allowed = machine.get(from_state)
    if allowed is None:
        allowed = machine.get(_state_value(from_state))
    if allowed is None or to_state not in allowed and _state_value(to_state) not in allowed:
        raise NodeExecutionError(
            ErrorCode.workflow_invalid_transition,
            f"Invalid {kind} transition: {_state_value(from_state)} -> {_state_value(to_state)}.",
            details={"kind": kind, "from": _state_value(from_state), "to": _state_value(to_state)},
        )
