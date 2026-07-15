"""Unit tests for the Temporal control-plane hardening (issue #69).

These exercise ``TemporalRuntimeAdapter`` without a real Temporal server by
monkeypatching ``Client.connect``: connect timeout, client reuse across calls,
``WorkflowAlreadyStartedError`` idempotency, and ``close()`` teardown. The real
end-to-end behaviour is covered by the gated ``tests/temporal`` suite.
"""

from __future__ import annotations

import asyncio

import pytest
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from packages.core import contracts as c
from packages.core.storage import Repository
from packages.core.workflow import temporal_adapter as ta
from packages.core.workflow.runtime import NodeExecutionError, WorkflowRuntimeSettings
from packages.production.pipeline.reuse import ReusePlan


def _adapter() -> ta.TemporalRuntimeAdapter:
    return ta.TemporalRuntimeAdapter(WorkflowRuntimeSettings(runtime="temporal"))


def _node(node_id: str, *, attempts: int = 1) -> c.NodeSpec:
    return c.NodeSpec(
        node_id=node_id,
        output_artifact_kinds=[c.ArtifactKind.run_report_debug],
        retry_policy=c.RetryPolicy(
            max_attempts=attempts,
            backoff_seconds=0.2,
            backoff_multiplier=1.5,
        ),
    )


def _job_and_run() -> tuple[c.Job, c.WorkflowRun]:
    job = c.Job(
        id="job_temporal",
        type=c.JobType.digital_human_video,
        status=c.JobStatus.running,
        case_id="case_1",
        created_by="usr_1",
        request_schema="DigitalHumanVideoRequest.v1",
        request=c.DigitalHumanVideoRequest(
            case_id="case_1",
            title="Temporal",
            script="脚本",
            voice={"voice_id": "voice_sandbox"},
        ),
    )
    run = c.WorkflowRun(
        id="run_temporal",
        job_id=job.id,
        case_id="case_1",
        workflow_template_id="test_template",
        workflow_version="v1",
        status=c.RunStatus.running,
        requested_by="usr_1",
    )
    return job, run


def _template() -> c.WorkflowTemplate:
    return c.WorkflowTemplate(
        workflow_template_id="test_template",
        version="v1",
        nodes=[
            _node("ValidateRequest", attempts=3),
            _node("LipSync"),
            _node("SeedanceGenerateVideo"),
        ],
    )


def test_adapter_reuses_a_single_client_across_calls(monkeypatch):
    connects = {"count": 0}

    class _FakeClient:
        pass

    async def _fake_connect(*_args, **_kwargs):
        connects["count"] += 1
        return _FakeClient()

    monkeypatch.setattr(Client, "connect", _fake_connect)
    adapter = _adapter()
    try:
        first = adapter._run(adapter._client())
        second = adapter._run(adapter._client())
        assert first is second
        assert connects["count"] == 1
    finally:
        adapter.close()


def test_adapter_connect_timeout_surfaces_worker_lost(monkeypatch):
    monkeypatch.setattr(ta, "TEMPORAL_CONNECT_TIMEOUT_SECONDS", 0.05)

    async def _slow_connect(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(Client, "connect", _slow_connect)
    adapter = _adapter()
    try:
        with pytest.raises(NodeExecutionError) as excinfo:
            adapter._run(adapter._client())
        assert excinfo.value.error.code == c.ErrorCode.workflow_worker_lost
    finally:
        adapter.close()


def test_start_workflow_is_idempotent_on_already_started(monkeypatch):
    class _FakeClient:
        async def start_workflow(self, *_args, **_kwargs):
            raise WorkflowAlreadyStartedError(
                workflow_id="run_x", workflow_type=ta.WORKFLOW_TYPE, run_id="run_x"
            )

    async def _fake_connect(*_args, **_kwargs):
        return _FakeClient()

    monkeypatch.setattr(Client, "connect", _fake_connect)
    adapter = _adapter()
    try:
        # A workflow already existing for this run_id is treated as success
        # (idempotent create retry), so no exception escapes.
        assert adapter._run(adapter._start_workflow({"run_id": "run_x"})) is None
    finally:
        adapter.close()


def test_start_workflow_passes_rpc_timeout(monkeypatch):
    seen = {}

    class _FakeClient:
        async def start_workflow(self, *_args, **kwargs):
            seen.update(kwargs)

    async def _fake_connect(*_args, **_kwargs):
        return _FakeClient()

    monkeypatch.setattr(Client, "connect", _fake_connect)
    adapter = _adapter()
    try:
        adapter._run(adapter._start_workflow({"run_id": "run_y"}))
        assert seen.get("rpc_timeout") == ta.TEMPORAL_RPC_TIMEOUT
        assert seen.get("task_queue") == adapter.settings.temporal_task_queue
    finally:
        adapter.close()


def test_close_is_idempotent_and_safe_without_a_loop():
    adapter = _adapter()
    # Never started a loop -> close must be a no-op, and calling twice is safe.
    adapter.close()
    adapter.close()


def test_start_run_and_resume_build_control_plane_payloads(monkeypatch):
    job, run = _job_and_run()
    template = _template()
    repo = Repository()
    repo.jobs[job.id] = job
    repo.runs[run.id] = run
    adapter = ta.TemporalRuntimeAdapter(WorkflowRuntimeSettings(runtime="temporal"), repository=repo)
    payloads: list[dict] = []

    async def fake_start(payload):
        payloads.append(payload)

    monkeypatch.setattr(adapter, "_start_workflow", fake_start)
    monkeypatch.setattr(adapter, "_run", lambda coro: asyncio.run(coro))

    adapter.start_run(job=job, run=run, template=template)

    assert payloads[0]["job_id"] == job.id
    assert payloads[0]["run_id"] == run.id
    assert payloads[0]["nodes"][0]["retry_policy"]["max_attempts"] == 3
    assert payloads[0]["nodes"][0]["timeout_seconds"] == 30 * 60
    assert payloads[0]["nodes"][1]["timeout_seconds"] == 120 * 60
    assert payloads[0]["nodes"][2]["timeout_seconds"] == 60 * 60

    monkeypatch.setattr(ta, "_template_from_run", lambda _run: template)
    new_run = run.model_copy(update={"id": "run_resumed"})
    repo.runs[new_run.id] = new_run
    adapter.resume_run(
        source_run_id=run.id,
        new_run=new_run,
        reuse_plan=ReusePlan(source_run_id=run.id, reused_node_ids=["ValidateRequest"]),
    )

    assert payloads[1]["source_run_id"] == run.id
    assert payloads[1]["reuse_plan"]["reused_node_ids"] == ["ValidateRequest"]


def test_resume_requires_api_repository_job() -> None:
    _job, run = _job_and_run()
    adapter = _adapter()

    with pytest.raises(RuntimeError, match="requires the API runtime repository"):
        adapter.resume_run(source_run_id="run_source", new_run=run, reuse_plan={"source_run_id": "run_source"})


def test_cancel_run_signals_and_leaves_running_run_cancelling(monkeypatch):
    job, run = _job_and_run()
    repo = Repository()
    repo.jobs[job.id] = job
    repo.runs[run.id] = run
    adapter = ta.TemporalRuntimeAdapter(WorkflowRuntimeSettings(runtime="temporal"), repository=repo)
    calls: list[dict] = []

    async def fake_cancel(run_id, *, force, reason):
        calls.append({"run_id": run_id, "force": force, "reason": reason})

    monkeypatch.setattr(adapter, "_cancel_workflow", fake_cancel)
    monkeypatch.setattr(adapter, "_run", lambda coro: asyncio.run(coro))

    assert adapter.cancel_run(run.id, reason="operator").status == c.RunStatus.cancelling
    assert repo.jobs[job.id].status == c.JobStatus.running
    repo.runs[run.id] = run
    forced = adapter.cancel_run(run.id, force=True, reason="operator")

    assert calls == [
        {"run_id": run.id, "force": False, "reason": "operator"},
        {"run_id": run.id, "force": True, "reason": "operator"},
    ]
    assert forced.status == c.RunStatus.cancelling
    assert repo.jobs[job.id].status == c.JobStatus.running


def test_local_cancelling_does_not_rewrite_terminal_runs() -> None:
    job, run = _job_and_run()
    repo = Repository()
    repo.jobs[job.id] = job.model_copy(update={"status": c.JobStatus.succeeded})
    repo.runs[run.id] = run.model_copy(update={"status": c.RunStatus.succeeded})
    adapter = ta.TemporalRuntimeAdapter(WorkflowRuntimeSettings(runtime="temporal"), repository=repo)

    adapter._mark_local_cancelling(run.id)
    adapter._mark_local_cancelling("missing")

    assert repo.runs[run.id].status == c.RunStatus.succeeded
    assert repo.jobs[job.id].status == c.JobStatus.succeeded


def test_cancel_does_not_signal_when_sql_commit_already_finished_run(monkeypatch) -> None:
    job, run = _job_and_run()
    repo = Repository()
    repo.jobs[job.id] = job
    repo.runs[run.id] = run

    class _ProductionRepository:
        def request_run_cancellation(self, run_id: str, *, force: bool):
            assert run_id == run.id
            assert force is False
            return run.model_copy(update={"status": c.RunStatus.succeeded})

    adapter = ta.TemporalRuntimeAdapter(
        WorkflowRuntimeSettings(runtime="temporal"),
        repository=repo,
        production_repository=_ProductionRepository(),
    )
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda _coro: pytest.fail("completed workflow must not receive a cancel signal"),
    )

    result = adapter.cancel_run(run.id)

    assert result.status == c.RunStatus.succeeded
    assert repo.runs[run.id].status == c.RunStatus.succeeded


def test_sql_cancel_uses_durable_state_when_api_cache_is_stale(monkeypatch) -> None:
    job, run = _job_and_run()
    repo = Repository()
    repo.jobs[job.id] = job.model_copy(update={"status": c.JobStatus.succeeded})
    repo.runs[run.id] = run.model_copy(update={"status": c.RunStatus.succeeded})

    class _ProductionRepository:
        def request_run_cancellation(self, run_id: str, *, force: bool):
            assert run_id == run.id
            assert force is False
            return run.model_copy(update={"status": c.RunStatus.cancelling})

        def run_cancel_mode(self, _run_id: str) -> str:
            return "graceful"

    adapter = ta.TemporalRuntimeAdapter(
        WorkflowRuntimeSettings(runtime="temporal"),
        repository=repo,
        production_repository=_ProductionRepository(),
    )
    calls: list[str] = []

    async def fake_cancel(run_id: str, *, force: bool, reason: str | None):
        assert force is False
        calls.append(run_id)

    monkeypatch.setattr(adapter, "_cancel_workflow", fake_cancel)
    monkeypatch.setattr(adapter, "_run", lambda coro: asyncio.run(coro))

    result = adapter.cancel_run(run.id)

    assert result.status == c.RunStatus.cancelling
    assert calls == [run.id]


def test_sql_force_escalation_cannot_be_downgraded_by_later_graceful_signal(
    monkeypatch,
) -> None:
    job, run = _job_and_run()
    repo = Repository()
    repo.jobs[job.id] = job
    repo.runs[run.id] = run

    class _ProductionRepository:
        def request_run_cancellation(self, _run_id: str, *, force: bool):
            assert force is False
            return run.model_copy(update={"status": c.RunStatus.cancelling})

        def run_cancel_mode(self, _run_id: str) -> str:
            return "force"

    adapter = ta.TemporalRuntimeAdapter(
        WorkflowRuntimeSettings(runtime="temporal"),
        repository=repo,
        production_repository=_ProductionRepository(),
    )
    calls: list[bool] = []

    async def fake_cancel(_run_id: str, *, force: bool, reason: str | None):
        calls.append(force)

    monkeypatch.setattr(adapter, "_cancel_workflow", fake_cancel)
    monkeypatch.setattr(adapter, "_run", lambda coro: asyncio.run(coro))

    adapter.cancel_run(run.id, force=False)

    assert calls == [True]


def test_force_cancel_uses_signal_with_force_mode(monkeypatch):
    seen: dict[str, object] = {}

    class _Handle:
        async def signal(self, name, payload, **kwargs):
            seen.update(name=name, payload=payload, kwargs=kwargs)

    class _Client:
        def get_workflow_handle(self, run_id):
            seen["run_id"] = run_id
            return _Handle()

    adapter = _adapter()

    async def fake_client():
        return _Client()

    monkeypatch.setattr(adapter, "_client", fake_client)
    adapter._run(adapter._cancel_workflow("run_force", force=True, reason="operator"))
    adapter.close()

    assert seen["run_id"] == "run_force"
    assert seen["name"] == "cancel"
    assert seen["payload"] == {"mode": "force", "reason": "operator"}
    assert seen["kwargs"] == {"rpc_timeout": ta.TEMPORAL_RPC_TIMEOUT}


def test_client_connect_os_error_surfaces_worker_lost(monkeypatch):
    async def broken_connect(*_args, **_kwargs):
        raise OSError("refused")

    monkeypatch.setattr(Client, "connect", broken_connect)
    adapter = _adapter()
    try:
        with pytest.raises(NodeExecutionError) as excinfo:
            adapter._run(adapter._client())
        assert excinfo.value.error.code == c.ErrorCode.workflow_worker_lost
        assert "Cannot reach Temporal" in excinfo.value.error.message
    finally:
        adapter.close()


def test_retry_policy_and_template_from_run_guards(monkeypatch):
    policy = ta._retry_policy({"backoff_seconds": 0, "backoff_multiplier": 3, "max_attempts": 4})

    assert policy.initial_interval.total_seconds() == 1
    assert policy.backoff_coefficient == 3
    assert policy.maximum_attempts == 4

    mismatched = _template().model_copy(update={"version": "v2"})
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.template_for",
        lambda _template_id: mismatched,
    )
    _job, run = _job_and_run()
    with pytest.raises(RuntimeError, match="unsupported template"):
        ta._template_from_run(run)


def test_admit_case_runs_without_production_repository_returns_empty(monkeypatch):
    monkeypatch.setattr(
        ta,
        "_activity_context",
        ta.TemporalActivityContext(
            repository=Repository(),
            local_runtime=object(),
            production_repository=None,
        ),
    )

    assert ta.admit_case_runs({"case_id": "case_no_sql"}) == {
        "case_id": "case_no_sql",
        "admitted_run_ids": [],
        "active_count": 0,
        "queued_remaining": 0,
    }


def test_admit_case_runs_starts_selected_rows_and_updates_summary(monkeypatch):
    job, run = _job_and_run()

    class _ProductionRepository:
        def __init__(self) -> None:
            self.started: list[str] = []

        def admit_case_runs(self, *, case_id: str, max_inflight: int):
            assert case_id == "case_1"
            assert max_inflight == 2
            return {
                "admitted": [(job, run)],
                "active_count": 1,
                "queued_remaining": 3,
            }

        def mark_run_started(self, run_id: str) -> None:
            self.started.append(run_id)

    production_repository = _ProductionRepository()
    monkeypatch.setattr(
        ta,
        "_activity_context",
        ta.TemporalActivityContext(
            repository=Repository(),
            local_runtime=object(),
            production_repository=production_repository,
        ),
    )
    monkeypatch.setattr(
        ta,
        "load_workflow_runtime_settings",
        lambda: WorkflowRuntimeSettings(runtime="temporal", case_max_inflight_runs=2),
    )

    async def fake_connect(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(Client, "connect", fake_connect)

    async def fake_start(_settings, _client, *, job, run):
        assert job.id == "job_temporal"
        assert run.id == "run_temporal"

    monkeypatch.setattr(ta, "_start_admitted_workflow", fake_start)

    assert ta.admit_case_runs({"case_id": "case_1"}) == {
        "case_id": "case_1",
        "admitted_run_ids": ["run_temporal"],
        "start_error_run_ids": [],
        "active_count": 2,
        "queued_remaining": 2,
    }
    assert production_repository.started == ["run_temporal"]


def test_start_admitted_workflow_treats_existing_run_as_success(monkeypatch):
    job, run = _job_and_run()
    seen: dict[str, object] = {}

    class _FakeClient:
        async def start_workflow(self, workflow_type, payload, **kwargs):
            seen["workflow_type"] = workflow_type
            seen["payload"] = payload
            seen["kwargs"] = kwargs
            raise WorkflowAlreadyStartedError(
                workflow_id=run.id,
                workflow_type=workflow_type,
                run_id=run.id,
            )

    monkeypatch.setattr(ta, "_template_from_run", lambda _run: _template())

    asyncio.run(
        ta._start_admitted_workflow(
            WorkflowRuntimeSettings(runtime="temporal", temporal_task_queue="queue-a"),
            _FakeClient(),
            job=job,
            run=run,
        )
    )

    assert seen["workflow_type"] == ta.WORKFLOW_TYPE
    assert seen["payload"]["run_id"] == run.id
    assert seen["kwargs"]["id"] == run.id
    assert seen["kwargs"]["task_queue"] == "queue-a"
    assert seen["kwargs"]["rpc_timeout"] == ta.TEMPORAL_RPC_TIMEOUT


def test_admit_case_runs_shares_one_client_across_batch(monkeypatch):
    """R2: a whole admitted batch is started over a single Temporal client."""
    job_a, run_a = _job_and_run()
    run_b = run_a.model_copy(update={"id": "run_temporal_b"})

    class _ProductionRepository:
        def __init__(self) -> None:
            self.started: list[str] = []

        def admit_case_runs(self, *, case_id: str, max_inflight: int):
            return {
                "admitted": [(job_a, run_a), (job_a, run_b)],
                "active_count": 0,
                "queued_remaining": 2,
            }

        def mark_run_started(self, run_id: str) -> None:
            self.started.append(run_id)

    production_repository = _ProductionRepository()
    monkeypatch.setattr(
        ta,
        "_activity_context",
        ta.TemporalActivityContext(
            repository=Repository(),
            local_runtime=object(),
            production_repository=production_repository,
        ),
    )
    monkeypatch.setattr(
        ta,
        "load_workflow_runtime_settings",
        lambda: WorkflowRuntimeSettings(runtime="temporal", case_max_inflight_runs=2),
    )

    connects = {"count": 0}
    started_run_ids: list[str] = []

    class _FakeClient:
        async def start_workflow(self, _workflow_type, payload, **_kwargs):
            started_run_ids.append(payload["run_id"])

    async def fake_connect(*_args, **_kwargs):
        connects["count"] += 1
        return _FakeClient()

    monkeypatch.setattr(Client, "connect", fake_connect)
    monkeypatch.setattr(ta, "_template_from_run", lambda _run: _template())

    summary = ta.admit_case_runs({"case_id": "case_1"})

    assert connects["count"] == 1
    assert started_run_ids == ["run_temporal", "run_temporal_b"]
    assert production_repository.started == ["run_temporal", "run_temporal_b"]
    assert summary["admitted_run_ids"] == ["run_temporal", "run_temporal_b"]
    assert summary["start_error_run_ids"] == []
    assert summary["active_count"] == 2
    assert summary["queued_remaining"] == 0


def test_admit_case_runs_connect_failure_leaves_whole_batch_admitted(monkeypatch):
    """R2: an unreachable Temporal leaves every admitted run queued for retry."""
    job_a, run_a = _job_and_run()
    run_b = run_a.model_copy(update={"id": "run_temporal_b"})

    class _ProductionRepository:
        def __init__(self) -> None:
            self.started: list[str] = []

        def admit_case_runs(self, *, case_id: str, max_inflight: int):
            return {
                "admitted": [(job_a, run_a), (job_a, run_b)],
                "active_count": 0,
                "queued_remaining": 2,
            }

        def mark_run_started(self, run_id: str) -> None:
            self.started.append(run_id)

    production_repository = _ProductionRepository()
    monkeypatch.setattr(
        ta,
        "_activity_context",
        ta.TemporalActivityContext(
            repository=Repository(),
            local_runtime=object(),
            production_repository=production_repository,
        ),
    )
    monkeypatch.setattr(
        ta,
        "load_workflow_runtime_settings",
        lambda: WorkflowRuntimeSettings(runtime="temporal", case_max_inflight_runs=2),
    )

    async def broken_connect(*_args, **_kwargs):
        raise OSError("refused")

    monkeypatch.setattr(Client, "connect", broken_connect)

    summary = ta.admit_case_runs({"case_id": "case_1"})

    assert production_repository.started == []
    assert summary["admitted_run_ids"] == []
    assert summary["start_error_run_ids"] == ["run_temporal", "run_temporal_b"]
    # No run was started, so active stays 0 and the full queue remains.
    assert summary["active_count"] == 0
    assert summary["queued_remaining"] == 2


def test_admission_summary_is_idle_only_when_no_queue_and_no_active():
    """R1: the controller's terminal predicate — no queued and no running runs."""
    assert ta._admission_summary_is_idle({"queued_remaining": 0, "active_count": 0})
    assert ta._admission_summary_is_idle({})
    assert not ta._admission_summary_is_idle({"queued_remaining": 1, "active_count": 0})
    assert not ta._admission_summary_is_idle({"queued_remaining": 0, "active_count": 2})
    assert not ta._admission_summary_is_idle({"queued_remaining": 3, "active_count": 4})
    # Missing/None fields coerce to 0 so a partial summary still reads as idle.
    assert ta._admission_summary_is_idle({"queued_remaining": None, "active_count": None})


def test_signal_case_admission_starts_or_pokes_existing_controller():
    calls: list[tuple[str, object]] = []

    class _Handle:
        async def signal(self, name, payload, **kwargs):
            calls.append((name, payload))
            assert kwargs["rpc_timeout"] == ta.TEMPORAL_RPC_TIMEOUT

    class _ClientStartsNew:
        async def start_workflow(self, workflow_type, payload, **kwargs):
            calls.append((workflow_type, payload))
            assert kwargs["id"] == "case-run-admission:case_1"
            assert kwargs["task_queue"] == "queue-b"
            assert kwargs["rpc_timeout"] == ta.TEMPORAL_RPC_TIMEOUT

    class _ClientAlreadyStarted:
        async def start_workflow(self, workflow_type, payload, **kwargs):
            raise WorkflowAlreadyStartedError(
                workflow_id=kwargs["id"],
                workflow_type=workflow_type,
                run_id=kwargs["id"],
            )

        def get_workflow_handle(self, workflow_id):
            calls.append(("handle", workflow_id))
            return _Handle()

    settings = WorkflowRuntimeSettings(runtime="temporal", temporal_task_queue="queue-b")

    asyncio.run(ta.signal_case_admission_with_client(_ClientStartsNew(), settings, "case_1"))
    asyncio.run(ta.signal_case_admission_with_client(_ClientAlreadyStarted(), settings, "case_1"))

    assert calls == [
        (ta.CASE_ADMISSION_WORKFLOW_TYPE, {"case_id": "case_1"}),
        ("handle", "case-run-admission:case_1"),
        ("poke", {"case_id": "case_1"}),
    ]
