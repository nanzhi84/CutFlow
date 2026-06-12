from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy as TemporalRetryPolicy

# Domain modules are data/typing + activity-side only; the workflow body never
# calls their non-deterministic code paths, so they bypass sandbox validation.
with workflow.unsafe.imports_passed_through():
    from packages.core.observability import (
        bind_observability_context,
        record_temporal_activity_failure,
        reset_observability_context,
    )
    from packages.core.contracts import Job, RunStatus, WorkflowRun, WorkflowTemplate
    from packages.core.storage import Repository
    from packages.core.workflow.runtime import WorkflowRuntimeSettings
    from packages.production.pipeline import LocalRuntimeAdapter, ReusePlan
    from packages.production.sqlalchemy_repository import SqlAlchemyProductionRepository


WORKFLOW_TYPE = "DigitalHumanVideoWorkflow"


@dataclass
class TemporalActivityContext:
    repository: Repository
    local_runtime: LocalRuntimeAdapter
    production_repository: SqlAlchemyProductionRepository | None = None


_activity_context: TemporalActivityContext | None = None


def configure_temporal_activity_context(context: TemporalActivityContext) -> None:
    global _activity_context
    _activity_context = context


def temporal_workflows() -> list[type]:
    return [DigitalHumanVideoWorkflow]


def temporal_activities() -> list:
    return [apply_reuse_plan, run_node, mark_run_cancelled]


def _context() -> TemporalActivityContext:
    if _activity_context is None:
        raise RuntimeError("Temporal activity context has not been configured.")
    return _activity_context


@workflow.defn(name=WORKFLOW_TYPE)
class DigitalHumanVideoWorkflow:
    def __init__(self) -> None:
        self.cancel_requested = False
        self.current_status = RunStatus.admitted.value

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str(payload["run_id"])
        nodes = list(payload["nodes"])
        reuse_plan = payload.get("reuse_plan")
        start_index = 0
        if reuse_plan:
            if self.cancel_requested:
                return await self._cancel(run_id)
            reuse_summary = await workflow.execute_activity(
                "apply_reuse_plan",
                {
                    "run_id": run_id,
                    "source_run_id": payload.get("source_run_id"),
                    "reuse_plan": reuse_plan,
                },
                start_to_close_timeout=timedelta(minutes=5),
            )
            start_index = len(reuse_summary.get("reused_node_ids", []))

        for node in nodes[start_index:]:
            if self.cancel_requested:
                return await self._cancel(run_id)
            result = await workflow.execute_activity(
                "run_node",
                {"run_id": run_id, "node_id": node["node_id"]},
                start_to_close_timeout=timedelta(seconds=node["timeout_seconds"]),
                retry_policy=_retry_policy(node["retry_policy"]),
            )
            self.current_status = str(result.get("run_status") or self.current_status)
            if self.current_status in {
                RunStatus.failed.value,
                RunStatus.cancelled.value,
                RunStatus.succeeded.value,
            }:
                return {"run_id": run_id, "status": self.current_status}
        return {"run_id": run_id, "status": self.current_status}

    @workflow.signal(name="cancel")
    async def cancel(self, payload: dict[str, Any] | None = None) -> None:
        self.cancel_requested = True
        self.current_status = RunStatus.cancelling.value

    @workflow.query(name="status")
    def status(self) -> str:
        return self.current_status

    async def _cancel(self, run_id: str) -> dict[str, Any]:
        result = await workflow.execute_activity(
            "mark_run_cancelled",
            {"run_id": run_id},
            start_to_close_timeout=timedelta(minutes=2),
        )
        self.current_status = RunStatus.cancelled.value
        return {"run_id": run_id, "status": result["run_status"]}


def _retry_policy(policy: dict[str, Any]) -> TemporalRetryPolicy:
    return TemporalRetryPolicy(
        initial_interval=timedelta(seconds=max(1, float(policy.get("backoff_seconds") or 1))),
        backoff_coefficient=float(policy.get("backoff_multiplier") or 2.0),
        maximum_attempts=int(policy.get("max_attempts") or 1),
    )


@activity.defn(name="apply_reuse_plan")
def apply_reuse_plan(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    run_id = str(payload["run_id"])
    source_run_id = str(payload["source_run_id"])
    token = _bind_activity_context(ctx, run_id)
    try:
        if ctx.production_repository is not None:
            ctx.production_repository.hydrate_workflow_runtime_snapshot(ctx.repository, run_id)
        summary = ctx.local_runtime.apply_reuse_plan(
            run_id,
            source_run_id,
            ReusePlan.model_validate(payload["reuse_plan"]),
        )
        _sync_if_configured(ctx, run_id)
        return summary
    except Exception:
        record_temporal_activity_failure()
        raise
    finally:
        reset_observability_context(token)


@activity.defn(name="run_node")
def run_node(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    run_id = str(payload["run_id"])
    node_id = str(payload["node_id"])
    token = _bind_activity_context(ctx, run_id, node_id=node_id)
    try:
        if ctx.production_repository is not None:
            ctx.production_repository.hydrate_workflow_runtime_snapshot(ctx.repository, run_id)
        activity.heartbeat({"run_id": run_id, "node_id": node_id, "phase": "started"})
        summary = ctx.local_runtime.run_node_activity(run_id, node_id)
        _sync_if_configured(ctx, run_id)
        activity.heartbeat({"run_id": run_id, "node_id": node_id, "phase": "finished"})
        return summary
    except Exception:
        record_temporal_activity_failure()
        raise
    finally:
        reset_observability_context(token)


@activity.defn(name="mark_run_cancelled")
def mark_run_cancelled(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    run_id = str(payload["run_id"])
    token = _bind_activity_context(ctx, run_id)
    try:
        if ctx.production_repository is not None:
            ctx.production_repository.hydrate_workflow_runtime_snapshot(ctx.repository, run_id)
        run = ctx.local_runtime.request_cancel(run_id)
        _sync_if_configured(ctx, run_id)
        return {"run_id": run.id, "run_status": run.status.value}
    except Exception:
        record_temporal_activity_failure()
        raise
    finally:
        reset_observability_context(token)


def _bind_activity_context(ctx: TemporalActivityContext, run_id: str, node_id: str | None = None):
    run = ctx.repository.runs.get(run_id)
    return bind_observability_context(
        job_id=run.job_id if run is not None else None,
        run_id=run_id,
        node_run_id=node_id,
    )


def _sync_if_configured(ctx: TemporalActivityContext, run_id: str) -> None:
    if ctx.production_repository is None:
        return
    run = ctx.repository.runs[run_id]
    ctx.production_repository.sync_workflow_snapshot(
        job=ctx.repository.jobs[run.job_id],
        run=run,
        repository=ctx.repository,
    )


class TemporalRuntimeAdapter:
    def __init__(
        self,
        settings: WorkflowRuntimeSettings,
        *,
        repository: Repository | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository

    def start_run(self, *, job: Job, run: WorkflowRun, template: WorkflowTemplate) -> None:
        self._run(
            self._start_workflow(
                _workflow_payload(job=job, run=run, template=template, reuse_plan=None)
            )
        )

    def resume_run(
        self,
        *,
        source_run_id: str,
        new_run: WorkflowRun,
        reuse_plan,
    ) -> None:
        job = self.repository.jobs[new_run.job_id] if self.repository is not None else None
        if job is None:
            raise RuntimeError("Temporal resume requires the API runtime repository.")
        self._run(
            self._start_workflow(
                _workflow_payload(
                    job=job,
                    run=new_run,
                    template=_template_from_run(new_run),
                    source_run_id=source_run_id,
                    reuse_plan=ReusePlan.model_validate(reuse_plan),
                )
            )
        )

    def cancel_run(
        self, run_id: str, *, force: bool = False, reason: str | None = None
    ) -> WorkflowRun | None:
        self._run(self._cancel_workflow(run_id, force=force, reason=reason))
        if force:
            self._mark_local_force_cancelled(run_id)
        return self.repository.runs.get(run_id) if self.repository is not None else None

    def get_run_status(self, run_id: str) -> RunStatus | None:
        value = self._run(self._query_status(run_id))
        return RunStatus(value) if value else None

    async def _client(self) -> Client:
        return await Client.connect(
            self.settings.temporal_address,
            namespace=self.settings.temporal_namespace,
        )

    async def _start_workflow(self, payload: dict[str, Any]) -> None:
        client = await self._client()
        await client.start_workflow(
            WORKFLOW_TYPE,
            payload,
            id=str(payload["run_id"]),
            task_queue=self.settings.temporal_task_queue,
        )

    async def _cancel_workflow(self, run_id: str, *, force: bool, reason: str | None) -> None:
        client = await self._client()
        handle = client.get_workflow_handle(run_id)
        if force:
            await handle.terminate(reason=reason or "force cancel requested")
        else:
            await handle.signal("cancel", {"reason": reason or ""})

    async def _query_status(self, run_id: str) -> str | None:
        client = await self._client()
        handle = client.get_workflow_handle(run_id)
        return await handle.query("status")

    def _run(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        # Called from a thread that already owns an event loop (e.g. async
        # route / test harness): run on a private loop in a helper thread.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coroutine).result()

    def _mark_local_force_cancelled(self, run_id: str) -> None:
        if self.repository is None or run_id not in self.repository.runs:
            return
        from packages.core.contracts import JobStatus, utcnow

        run = self.repository.runs[run_id]
        if run.status not in {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}:
            self.repository.runs[run_id] = run.model_copy(
                update={"status": RunStatus.cancelled, "finished_at": utcnow(), "updated_at": utcnow()}
            )
        job = self.repository.jobs.get(run.job_id)
        if job is not None and job.status not in {
            JobStatus.succeeded,
            JobStatus.failed,
            JobStatus.cancelled,
            JobStatus.archived,
        }:
            self.repository.jobs[job.id] = job.model_copy(
                update={"status": JobStatus.cancelled, "updated_at": utcnow()}
            )


def _workflow_payload(
    *,
    job: Job,
    run: WorkflowRun,
    template: WorkflowTemplate,
    source_run_id: str | None = None,
    reuse_plan: ReusePlan | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "run_id": run.id,
        "workflow_template_id": template.workflow_template_id,
        "workflow_version": template.version,
        "source_run_id": source_run_id,
        "reuse_plan": reuse_plan.model_dump(mode="json") if reuse_plan else None,
        "nodes": [
            {
                "node_id": node.node_id,
                "retry_policy": node.retry_policy.model_dump(mode="json"),
                "timeout_seconds": _node_timeout_seconds(node.node_id),
            }
            for node in template.nodes
        ],
    }


def _template_from_run(run: WorkflowRun) -> WorkflowTemplate:
    from packages.production.pipeline.digital_human import digital_human_template

    template = digital_human_template()
    if (
        template.workflow_template_id != run.workflow_template_id
        or template.version != run.workflow_version
    ):
        raise RuntimeError(
            f"Run {run.id} uses unsupported template "
            f"{run.workflow_template_id}@{run.workflow_version}."
        )
    return template


def _node_timeout_seconds(node_id: str) -> int:
    if node_id == "LipSync":
        return 120 * 60
    return 30 * 60
