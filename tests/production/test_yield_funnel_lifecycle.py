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
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    JobStatus,
    JobType,
    NodeStatus,
    ProviderInvocation,
    ProviderStatus,
    RunStatus,
)
from packages.core.observability import FUNNEL_TAXONOMY
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.core.storage.repository import Repository
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


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
    # Run-level "succeeded" / "workflow_succeeded" are NOT §9.5 stages.
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

    def ok(_node_id, _run_arg, _node_run, _state_arg):
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


def test_subtitle_disabled_mix_still_emits_node_started():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    request = _request().model_copy(
        update={"subtitle": _request().subtitle.model_copy(update={"enabled": False})}
    )
    state = RunState(request=request)

    def ok(_node_id, _run_arg, _node_run, _state_arg):
        return NodeOutput(status=NodeStatus.succeeded)

    adapter._run_node = ok  # type: ignore[method-assign]

    assert adapter._execute_node("SubtitleAndBgmMix", run, state) is True
    assert "node_started" in _event_types(adapter.repository)


def test_execute_node_rejects_undeclared_output_kind():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    state = RunState(request=_request())

    def wrong_kind(_node_id, run_arg, node_run, _state_arg):
        artifact = adapter.repository.create_artifact(
            kind=ArtifactKind.provider_raw_request,
            payload_schema="UnexpectedArtifact.v1",
            payload={"unexpected": True},
            case_id=run_arg.case_id,
            run_id=run_arg.id,
            node_run_id=node_run.id,
        )
        return NodeOutput(artifacts=[artifact])

    adapter._run_node = wrong_kind  # type: ignore[method-assign]

    assert adapter._execute_node("ValidateRequest", run, state) is False
    failed = adapter.repository.node_runs[run.id][-1]
    assert failed.status == NodeStatus.failed
    assert failed.error is not None
    assert failed.error.code == ErrorCode.artifact_schema_mismatch
    assert failed.error.details["undeclared_kinds"] == ["provider.raw_request"]


def test_execute_node_retries_declared_retryable_error(monkeypatch):
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    state = RunState(request=_request())
    attempts = 0
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.time.sleep", lambda _delay: None
    )

    def conflict_then_ok(_node_id, _run_arg, _node_run, _state_arg):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise NodeExecutionError(
                ErrorCode.validation_conflict,
                "reservation race",
                retryable=True,
            )
        return NodeOutput(status=NodeStatus.succeeded)

    adapter._run_node = conflict_then_ok  # type: ignore[method-assign]

    assert adapter._execute_node("MaterialPackPlanning", run, state) is True
    assert attempts == 3
    assert adapter.repository.node_runs[run.id][-1].status == NodeStatus.succeeded


def test_reuse_prefix_carries_provider_warning_and_degradation_metadata():
    from packages.core.contracts import DegradationNotice, NodeRun, WarningCode, WorkflowRun
    from packages.production.pipeline.reuse import ReuseDecision, ReusePlan

    adapter, run, _ = _adapter_with_run(RunStatus.running)
    source_run = WorkflowRun(
        id="run_source",
        job_id=run.job_id,
        case_id=run.case_id,
        workflow_template_id=run.workflow_template_id,
        workflow_version=run.workflow_version,
        status=RunStatus.failed,
    )
    degradation = DegradationNotice(
        code=WarningCode.cover_frame_fallback,
        message="fallback",
        node_id="ValidateRequest",
    )
    source_node = NodeRun(
        id="nr_source",
        run_id=source_run.id,
        node_id="ValidateRequest",
        node_version="v1",
        status=NodeStatus.succeeded,
        input_manifest_hash="same",
        provider_invocation_ids=["pinv_source"],
        warnings=[WarningCode.cost_unpriced],
        degradations=[degradation],
    )
    adapter.repository.runs[source_run.id] = source_run
    adapter.repository.node_runs[source_run.id] = [source_node]
    state = RunState(
        request=_request(),
        provider_invocation_ids=["pinv_source"],
        warnings=[WarningCode.cost_unpriced],
        degradations=[degradation],
    )
    plan = ReusePlan(
        source_run_id=source_run.id,
        reused_node_ids=["ValidateRequest"],
        decisions=[ReuseDecision(node_id="ValidateRequest", reusable=True, reason="reused")],
    )

    assert adapter._reuse_prefix(run, state, source_run.id, plan) == 1
    assert state.provider_invocation_ids == ["pinv_source"]
    assert state.warnings == [WarningCode.cost_unpriced]
    assert state.degradations == [degradation]


def test_execute_node_degraded_counts_as_node_succeeded():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    state = RunState(request=_request())

    def degraded(_node_id, _run_arg, _node_run, _state_arg):
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

    def boom(_node_id, _run_arg, _node_run, _state_arg):
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


def test_node_failure_persists_provider_invocations_and_debug_artifacts():
    adapter, run, _ = _adapter_with_run(RunStatus.running)
    state = RunState(request=_request())

    def boom(_node_id, run_arg, node_run, _state_arg):
        artifact = adapter.repository.create_artifact(
            kind=ArtifactKind.provider_raw_response,
            payload_schema="TestProviderRawResponse.v1",
            payload={"raw": "model output"},
            case_id=run_arg.case_id,
            run_id=run_arg.id,
            node_run_id=node_run.id,
        )
        adapter.repository.provider_invocations["pinv_failed_node"] = ProviderInvocation(
            id="pinv_failed_node",
            case_id=run_arg.case_id,
            run_id=run_arg.id,
            node_run_id=node_run.id,
            provider_id="dashscope.llm",
            model_id="qwen3.7-plus",
            provider_profile_id="dashscope.llm.prod",
            capability_id="llm.chat",
            status=ProviderStatus.succeeded,
            response_artifact_id=artifact.id,
        )
        raise NodeExecutionError(ErrorCode.prompt_output_invalid, "bad model output")

    adapter._run_node = boom  # type: ignore[method-assign]
    proceeded = adapter._execute_node("ValidateRequest", run, state)

    assert proceeded is False
    failed_node = adapter.repository.node_runs[run.id][-1]
    assert failed_node.status == NodeStatus.failed
    assert failed_node.provider_invocation_ids == ["pinv_failed_node"]
    assert len(failed_node.output_artifact_ids) == 1
    debug_report = adapter.repository.artifacts[adapter.repository.runs[run.id].debug_report_artifact_id]
    assert debug_report.payload["provider_invocation_ids"] == ["pinv_failed_node"]
