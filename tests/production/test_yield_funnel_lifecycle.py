"""G3: the digital-human pipeline emits the §9.5 node/run funnel stages it owns.

The pipeline emits ``started`` (run begins running) and per-node
``node_started`` / ``node_succeeded`` / ``node_failed``. Run-level terminal
statuses (succeeded / cancelling / cancelled) are NOT §9.5 funnel stages and
must NOT be emitted — technical success is observed via node stages, true yield
via the publish stages ("成品率不得只看 workflow succeeded").

These drive the adapter's lifecycle methods directly against the in-memory
``Repository`` so they run without ffmpeg / the full node sequence. They also
prove the dependency-rule fix: digital_human imports the helper from
``packages.core.observability`` (never ``packages.ops``).
"""

from __future__ import annotations

from packages.core.contracts import (
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    JobStatus,
    JobType,
    NodeStatus,
    RunStatus,
)
from packages.core.observability import FUNNEL_TAXONOMY
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.core.storage.repository import Repository
from packages.production.pipeline.digital_human import LocalRuntimeAdapter, RunState


def _adapter_with_run(status: RunStatus) -> tuple[LocalRuntimeAdapter, "object", "object"]:
    repository = Repository()
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    job = Job(
        id="job_funnel",
        type=JobType.digital_human_video,
        status=JobStatus.running,
        case_id="case_demo",
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
        ),
    )
    from packages.core.contracts import WorkflowRun

    run = WorkflowRun(
        id="run_funnel",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=status,
    )
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    repository.node_runs[run.id] = []
    return adapter, run, job


def _event_types(repository: Repository) -> set[str]:
    return {event.event_type for event in repository.yield_events.values()}


def _request() -> DigitalHumanVideoRequest:
    return DigitalHumanVideoRequest(
        case_id="case_demo",
        script="hello",
        voice={"voice_id": "voice_sandbox"},
    )


def test_complete_run_does_not_emit_run_level_succeeded():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    adapter._complete_run(run.id)
    # Run-level "succeeded" / legacy "workflow_succeeded" are NOT §9.5 stages.
    types = _event_types(adapter.repository)
    assert "workflow_succeeded" not in types
    assert "succeeded" not in types
    assert types <= FUNNEL_TAXONOMY  # whatever was emitted is in-taxonomy


def test_mark_cancelled_does_not_emit_run_level_stages():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    adapter._mark_cancelled(run.id)
    types = _event_types(adapter.repository)
    assert "workflow_cancelling" not in types
    assert "workflow_cancelled" not in types
    assert "cancelling" not in types
    assert "cancelled" not in types


def test_mark_cancelled_releases_uncommitted_reservations_keeps_committed():
    # §6.6: cancel releases this run's uncommitted reservations so the slots are
    # reclaimable, but a committed pick stays as an audit/used marker.
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    adapter.repository.reserve_selections(
        case_id="case_demo",
        run_id=run.id,
        medium="portrait",
        asset_ids=["asset_portrait_demo", "asset_portrait_alt"],
    )
    adapter.repository.commit_selection_reservation(
        run_id=run.id, medium="portrait", asset_id="asset_portrait_demo"
    )
    adapter._mark_cancelled(run.id)
    statuses = {r.asset_id: r.status for r in adapter.repository.selection_reservations.values()}
    assert statuses["asset_portrait_demo"] == "committed"
    assert statuses["asset_portrait_alt"] == "released"


def test_execute_node_emits_node_started_and_node_succeeded():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    state = RunState(request=_request())

    def ok(node_id, run_arg, node_run, state_arg):
        return NodeOutput(status=NodeStatus.succeeded)

    adapter._run_node = ok  # type: ignore[method-assign]
    proceeded = adapter._execute_node("ValidateRequest", run, state)
    assert proceeded is True
    types = _event_types(adapter.repository)
    assert "node_started" in types
    assert "node_succeeded" in types
    assert "node_failed" not in types
    # node_succeeded must be keyed on the node run, not the run.
    succeeded = next(e for e in adapter.repository.yield_events.values() if e.event_type == "node_succeeded")
    assert succeeded.run_id == run.id
    assert succeeded.dedupe_key.endswith(":node_succeeded")


def test_execute_node_degraded_counts_as_node_succeeded():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    state = RunState(request=_request())

    def degraded(node_id, run_arg, node_run, state_arg):
        from packages.core.contracts import DegradationNotice, WarningCode

        return NodeOutput(
            status=NodeStatus.succeeded,
            degradations=[DegradationNotice(code=WarningCode.cover_frame_fallback, message="x")],
        )

    adapter._run_node = degraded  # type: ignore[method-assign]
    proceeded = adapter._execute_node("ValidateRequest", run, state)
    assert proceeded is True
    types = _event_types(adapter.repository)
    assert "node_succeeded" in types
    assert "node_failed" not in types


def test_node_failure_emits_node_failed_not_workflow_failed():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    state = RunState(request=_request())

    def boom(node_id, run_arg, node_run, state_arg):
        raise NodeExecutionError(ErrorCode.validation_invalid_options, "boom")

    adapter._run_node = boom  # type: ignore[method-assign]
    proceeded = adapter._execute_node("ValidateRequest", run, state)
    assert proceeded is False
    types = _event_types(adapter.repository)
    assert "node_started" in types
    assert "node_failed" in types
    assert "workflow_failed" not in types
    failed = next(e for e in adapter.repository.yield_events.values() if e.event_type == "node_failed")
    assert failed.run_id == run.id
    assert failed.dedupe_key.endswith(":node_failed")
