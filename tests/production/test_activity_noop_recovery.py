"""run_node_activity is a no-op when the node already reached a terminal status in
this run (issue #193 §6): a lost Temporal completion re-runs the activity, but the
paid work is done, so it returns the existing summary without re-entering the
gateway — even when the run itself already succeeded (which would otherwise trip
the job-status assert_transition).
"""

from __future__ import annotations

from packages.core.contracts import (
    DigitalHumanVideoRequest,
    Job,
    JobStatus,
    JobType,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.repository import Repository
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


class _ExplodingGateway:
    """Any invoke here means the activity wrongly re-executed the node."""

    def invoke(self, call):  # pragma: no cover - only hit on regression
        raise AssertionError("run_node_activity re-entered the gateway on a completed node")


def _adapter_with_terminal_node(
    *,
    node_id: str,
    node_status: NodeStatus = NodeStatus.succeeded,
    run_status: RunStatus = RunStatus.running,
    job_status: JobStatus = JobStatus.running,
) -> tuple[LocalRuntimeAdapter, WorkflowRun]:
    repository = Repository()
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    adapter.provider_gateway = _ExplodingGateway()
    job = Job(
        id="job_x",
        type=JobType.digital_human_video,
        status=job_status,
        case_id="case_demo",
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_demo", script="测试脚本。", voice={"voice_id": "voice_sandbox"}
        ),
    )
    run = WorkflowRun(
        id="run_x",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=run_status,
    )
    node = NodeRun(
        id=f"nr_{node_id}",
        run_id=run.id,
        node_id=node_id,
        node_version="v1",
        status=node_status,
        input_manifest_hash="h",
    )
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    repository.node_runs[run.id] = [node]
    return adapter, run


def test_replay_of_completed_node_returns_summary_without_gateway():
    adapter, run = _adapter_with_terminal_node(node_id="TTS")
    before = len(adapter.repository.node_runs[run.id])

    summary = adapter.run_node_activity(run.id, "TTS")

    assert summary["node_status"] == NodeStatus.succeeded.value
    assert summary["run_status"] == RunStatus.running.value
    # No new NodeRun appended: the node was not re-executed.
    assert len(adapter.repository.node_runs[run.id]) == before


def test_replay_of_last_node_on_succeeded_run_does_not_trip_transition():
    # Last node's snapshot committed (run already succeeded) but the Temporal
    # completion was lost. The pre-assert guard must return the summary instead of
    # letting assert_transition("job", succeeded, running) raise InvalidTransition.
    adapter, run = _adapter_with_terminal_node(
        node_id="FinalizeRunReport",
        run_status=RunStatus.succeeded,
        job_status=JobStatus.succeeded,
    )

    summary = adapter.run_node_activity(run.id, "FinalizeRunReport")

    assert summary["run_status"] == RunStatus.succeeded.value
    assert summary["node_status"] == NodeStatus.succeeded.value


def test_degraded_and_skipped_nodes_also_count_as_done():
    for status in (NodeStatus.degraded, NodeStatus.skipped):
        adapter, run = _adapter_with_terminal_node(node_id="LipSync", node_status=status)
        before = len(adapter.repository.node_runs[run.id])

        summary = adapter.run_node_activity(run.id, "LipSync")

        assert summary["node_status"] == status.value
        assert len(adapter.repository.node_runs[run.id]) == before


def test_terminal_predicate_matches_done_set_semantics():
    # The guard keys off "a terminal NodeRun exists for the canonical node", aligned
    # with _next_unfinished_node_id's done-set — pending/running/failed are not done.
    adapter, run = _adapter_with_terminal_node(node_id="TTS", node_status=NodeStatus.succeeded)
    assert adapter._node_already_terminal(run.id, "TTS") is True

    for status in (NodeStatus.pending, NodeStatus.running, NodeStatus.failed):
        pending_adapter, pending_run = _adapter_with_terminal_node(
            node_id="TTS", node_status=status
        )
        assert pending_adapter._node_already_terminal(pending_run.id, "TTS") is False

    # Clean-Slate does not let a retired persisted id satisfy an active node.
    retired_adapter, retired_run = _adapter_with_terminal_node(node_id="TimelinePlanning")
    assert (
        retired_adapter._node_already_terminal(
            retired_run.id, "TimelineAssemblyValidation"
        )
        is False
    )
