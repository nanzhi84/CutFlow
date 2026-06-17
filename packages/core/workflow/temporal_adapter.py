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
    from packages.ai.gateway import ProviderGateway
    from packages.ai.prompts import PromptRegistry
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

    def scoping_enabled(self) -> bool:
        """Per-activity Repository scoping applies only under the SQL backend.

        Without a ``production_repository`` there is no SQL hydrate/persist, so the
        worker is the pure in-memory runtime (single Repository, single run) and we
        keep the shared one to preserve existing behavior and passing tests.
        """
        return self.production_repository is not None

    def build_runtime(self) -> tuple[Repository, LocalRuntimeAdapter]:
        """Construct a FRESH, activity-scoped Repository + runtime.

        The mutable run-state Repository MUST NOT be shared across concurrent
        ``run_node`` activities (the worker runs an 8-thread activity pool): without
        this, reads/writes for different runs interleave on the same dicts ->
        cross-run data bleed + unbounded memory growth. We rebuild the mutable
        repository per activity but REUSE the stateless services (provider plugins
        and readers, secret/object stores, prompt reader) captured on the
        worker-global ``local_runtime`` so we avoid re-registering plugins or
        regenerating seed media on every invocation.
        """
        template = self.local_runtime
        repository = Repository()
        template_gateway = template.provider_gateway
        gateway = ProviderGateway(
            repository,
            provider_reader=template_gateway.provider_reader,
            secret_store=template_gateway.secret_store,
            object_store=template_gateway.object_store,
            http_client=template_gateway.http_client,
            budget_guard=template_gateway.budget_guard,
            circuit_breaker=template_gateway.circuit_breaker,
            auto_register_real_plugins=False,
        )
        # Reuse the already-registered (stateless) plugin instances rather than
        # re-registering real providers on every activity.
        gateway.plugins = dict(template_gateway.plugins)
        registry = PromptRegistry(
            repository,
            prompt_reader=template.prompt_registry.prompt_reader,
        )
        runtime = LocalRuntimeAdapter(
            repository,
            gateway,
            registry,
            seed_media=False,
        )
        return repository, runtime


_activity_context: TemporalActivityContext | None = None


def configure_temporal_activity_context(context: TemporalActivityContext) -> None:
    global _activity_context
    _activity_context = context


def temporal_workflows() -> list[type]:
    return [DigitalHumanVideoWorkflow]


def temporal_activities() -> list:
    return [apply_reuse_plan, run_node, mark_run_cancelled, mark_run_failed]


# A long node (e.g. LipSync) blocks the activity thread for minutes, so the
# activity heartbeats from a background thread every INTERVAL seconds; if those
# stop (worker lost), Temporal fails the activity after TIMEOUT seconds instead
# of waiting out the multi-hour start_to_close_timeout.
NODE_HEARTBEAT_INTERVAL_SECONDS = 20.0
NODE_HEARTBEAT_TIMEOUT_SECONDS = 90


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
        try:
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
                    heartbeat_timeout=timedelta(seconds=NODE_HEARTBEAT_TIMEOUT_SECONDS),
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
        except asyncio.CancelledError:
            raise
        except Exception:
            # A node activity was lost to an infrastructure failure (e.g. the
            # worker was restarted mid-node) and so never wrote a terminal status.
            # Reconcile the run to failed on a live worker so the UI reflects it
            # and an operator can resume — rather than leaving it stuck "running"
            # until the multi-hour start_to_close_timeout fires.
            await workflow.execute_activity(
                "mark_run_failed",
                {"run_id": run_id, "reason": "Worker lost or node activity timed out."},
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=TemporalRetryPolicy(maximum_attempts=5),
            )
            self.current_status = RunStatus.failed.value
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


def _activity_runtime(ctx: TemporalActivityContext) -> tuple[Repository, LocalRuntimeAdapter]:
    """Resolve the Repository + runtime for a single activity invocation.

    Under the SQL backend each activity gets a FRESH, isolated Repository so
    concurrent activities for different runs never share mutable run-state. The
    pure in-memory backend keeps the shared one (single-threaded per run).
    """
    if ctx.scoping_enabled():
        return ctx.build_runtime()
    return ctx.repository, ctx.local_runtime


@activity.defn(name="apply_reuse_plan")
def apply_reuse_plan(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    run_id = str(payload["run_id"])
    source_run_id = str(payload["source_run_id"])
    repository, runtime = _activity_runtime(ctx)
    if ctx.production_repository is not None:
        ctx.production_repository.hydrate_workflow_runtime_snapshot(repository, run_id)
    token = _bind_activity_context(repository, run_id)
    try:
        summary = runtime.apply_reuse_plan(
            run_id,
            source_run_id,
            ReusePlan.model_validate(payload["reuse_plan"]),
        )
        _sync_if_configured(ctx, repository, run_id)
        return summary
    except Exception:
        record_temporal_activity_failure()
        raise
    finally:
        reset_observability_context(token)


def _start_node_heartbeat(run_id: str, node_id: str):
    """Heartbeat the current activity from a daemon thread every
    ``NODE_HEARTBEAT_INTERVAL_SECONDS`` so a lost worker is detected within the
    activity's ``heartbeat_timeout`` even while a long node blocks the activity
    thread. Returns a callable that stops the thread.

    ``activity.heartbeat`` reads the activity context from a contextvar, so the
    thread runs it inside a copy of the current (activity) context.
    """
    import contextvars
    import threading

    stop = threading.Event()
    ctx = contextvars.copy_context()

    def _loop() -> None:
        while not stop.wait(NODE_HEARTBEAT_INTERVAL_SECONDS):
            try:
                ctx.run(activity.heartbeat, {"run_id": run_id, "node_id": node_id, "phase": "running"})
            except Exception:
                return

    thread = threading.Thread(target=_loop, name=f"hb-{run_id}-{node_id}", daemon=True)
    thread.start()

    def _stop() -> None:
        stop.set()
        thread.join(timeout=2)

    return _stop


@activity.defn(name="run_node")
def run_node(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    run_id = str(payload["run_id"])
    node_id = str(payload["node_id"])
    repository, runtime = _activity_runtime(ctx)
    if ctx.production_repository is not None:
        ctx.production_repository.hydrate_workflow_runtime_snapshot(repository, run_id)
    token = _bind_activity_context(repository, run_id, node_id=node_id)
    activity.heartbeat({"run_id": run_id, "node_id": node_id, "phase": "started"})
    stop_heartbeat = _start_node_heartbeat(run_id, node_id)
    try:
        summary = runtime.run_node_activity(run_id, node_id)
        _sync_if_configured(ctx, repository, run_id)
        activity.heartbeat({"run_id": run_id, "node_id": node_id, "phase": "finished"})
        return summary
    except Exception:
        record_temporal_activity_failure()
        raise
    finally:
        stop_heartbeat()
        reset_observability_context(token)


@activity.defn(name="mark_run_cancelled")
def mark_run_cancelled(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    run_id = str(payload["run_id"])
    repository, runtime = _activity_runtime(ctx)
    if ctx.production_repository is not None:
        ctx.production_repository.hydrate_workflow_runtime_snapshot(repository, run_id)
    token = _bind_activity_context(repository, run_id)
    try:
        run = runtime.request_cancel(run_id)
        _sync_if_configured(ctx, repository, run_id)
        return {"run_id": run.id, "run_status": run.status.value}
    except Exception:
        record_temporal_activity_failure()
        raise
    finally:
        reset_observability_context(token)


@activity.defn(name="mark_run_failed")
def mark_run_failed(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    run_id = str(payload["run_id"])
    reason = str(payload.get("reason") or "Worker lost or node activity timed out.")
    repository, runtime = _activity_runtime(ctx)
    if ctx.production_repository is not None:
        ctx.production_repository.hydrate_workflow_runtime_snapshot(repository, run_id)
    token = _bind_activity_context(repository, run_id)
    try:
        run = runtime.mark_run_failed(run_id, reason=reason)
        _sync_if_configured(ctx, repository, run_id)
        return {"run_id": run.id, "run_status": run.status.value}
    except Exception:
        record_temporal_activity_failure()
        raise
    finally:
        reset_observability_context(token)


def _bind_activity_context(repository: Repository, run_id: str, node_id: str | None = None):
    run = repository.runs.get(run_id)
    return bind_observability_context(
        job_id=run.job_id if run is not None else None,
        run_id=run_id,
        node_run_id=node_id,
    )


def _sync_if_configured(ctx: TemporalActivityContext, repository: Repository, run_id: str) -> None:
    if ctx.production_repository is None:
        return
    run = repository.runs[run_id]
    ctx.production_repository.sync_workflow_snapshot(
        job=repository.jobs[run.job_id],
        run=run,
        repository=repository,
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
