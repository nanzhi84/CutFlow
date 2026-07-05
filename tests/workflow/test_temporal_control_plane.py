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
        input_schema=f"{node_id}.input.v1",
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


def test_cancel_run_signals_or_force_terminates_and_updates_local_state(monkeypatch):
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

    assert adapter.cancel_run(run.id, reason="operator").status == c.RunStatus.cancelled
    assert repo.jobs[job.id].status == c.JobStatus.running
    repo.runs[run.id] = run
    forced = adapter.cancel_run(run.id, force=True, reason="operator")

    assert calls == [
        {"run_id": run.id, "force": False, "reason": "operator"},
        {"run_id": run.id, "force": True, "reason": "operator"},
    ]
    assert forced.status == c.RunStatus.cancelled
    assert repo.jobs[job.id].status == c.JobStatus.cancelled


def test_force_cancel_does_not_rewrite_terminal_runs() -> None:
    job, run = _job_and_run()
    repo = Repository()
    repo.jobs[job.id] = job.model_copy(update={"status": c.JobStatus.succeeded})
    repo.runs[run.id] = run.model_copy(update={"status": c.RunStatus.succeeded})
    adapter = ta.TemporalRuntimeAdapter(WorkflowRuntimeSettings(runtime="temporal"), repository=repo)

    adapter._mark_local_force_cancelled(run.id)
    adapter._mark_local_force_cancelled("missing")

    assert repo.runs[run.id].status == c.RunStatus.succeeded
    assert repo.jobs[job.id].status == c.JobStatus.succeeded


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
