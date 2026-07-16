from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy as TemporalRetryPolicy
from temporalio.exceptions import (
    ActivityError,
    CancelledError as TemporalCancelledError,
    WorkflowAlreadyStartedError,
)
from temporalio.service import RPCError
from temporalio.workflow import ActivityCancellationType

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
    from packages.core.contracts import ErrorCode, Job, RunStatus, WorkflowRun, WorkflowTemplate
    from packages.core.storage import Repository
    from packages.core.workflow.runtime import (
        cancellation_scope,
        ExecutionCancelled,
        NodeExecutionError,
        WorkflowRuntimeSettings,
        load_workflow_runtime_settings,
    )
    from packages.production.pipeline import LocalRuntimeAdapter, ReusePlan
    from packages.production.sqlalchemy_repository import SqlAlchemyProductionRepository


WORKFLOW_TYPE = "DigitalHumanVideoWorkflow"
CASE_ADMISSION_WORKFLOW_TYPE = "CaseRunAdmissionWorkflow"
CASE_ADMISSION_WORKFLOW_ID_PREFIX = "case-run-admission:"
logger = logging.getLogger(__name__)


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
            production_repository=self.production_repository,
        )
        return repository, runtime


_activity_context: TemporalActivityContext | None = None


def configure_temporal_activity_context(context: TemporalActivityContext) -> None:
    global _activity_context
    _activity_context = context


def temporal_workflows() -> list[type]:
    return [DigitalHumanVideoWorkflow, CaseRunAdmissionWorkflow]


def temporal_activities() -> list:
    return [
        apply_reuse_plan,
        run_node,
        mark_run_cancelled,
        mark_run_failed,
        admit_case_runs,
    ]


# A long node (e.g. LipSync) blocks the activity thread for minutes, so the
# activity heartbeats from a background thread every INTERVAL seconds; if those
# stop (worker lost), Temporal fails the activity after TIMEOUT seconds instead
# of waiting out the multi-hour start_to_close_timeout.
NODE_HEARTBEAT_INTERVAL_SECONDS = 20.0
NODE_HEARTBEAT_TIMEOUT_SECONDS = 90
CANCELLABLE_ACTIVITY_PATCH = "cancel-running-activity-v1"
CANCELLATION_DB_POLL_SECONDS = 0.25

# Control-plane (API request path) timeouts. The API creates/cancels runs by
# talking to Temporal from a synchronous request handler, so every call MUST be
# bounded: an unreachable or slow Temporal must surface a fast error instead of
# blocking the API worker. ``CONNECT`` bounds the one-time client connect,
# ``RPC`` bounds each start/signal/terminate gRPC call, and ``CALL`` is the
# outer wall-clock bound enforced on the calling thread (connect + rpc + margin).
TEMPORAL_CONNECT_TIMEOUT_SECONDS = 10.0
TEMPORAL_RPC_TIMEOUT = timedelta(seconds=30)
TEMPORAL_CALL_TIMEOUT_SECONDS = 45.0
CASE_ADMISSION_POLL_SECONDS = 30.0
CASE_ADMISSION_CONTINUE_AS_NEW_CYCLES = 500


def _context() -> TemporalActivityContext:
    if _activity_context is None:
        raise RuntimeError("Temporal activity context has not been configured.")
    return _activity_context


@workflow.defn(name=WORKFLOW_TYPE)
class DigitalHumanVideoWorkflow:
    def __init__(self) -> None:
        self.cancel_requested = False
        self.cancel_mode = "graceful"
        self.current_status = RunStatus.admitted.value
        self.current_activity = None

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
                    schedule_to_close_timeout=timedelta(minutes=20),
                    retry_policy=TemporalRetryPolicy(maximum_attempts=3),
                )
                start_index = len(reuse_summary.get("reused_node_ids", []))

            for node in nodes[start_index:]:
                if self.cancel_requested:
                    return await self._cancel(run_id)
                cancellation_type = (
                    ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
                    if workflow.patched(CANCELLABLE_ACTIVITY_PATCH)
                    else ActivityCancellationType.TRY_CANCEL
                )
                activity_handle = workflow.start_activity(
                    "run_node",
                    {"run_id": run_id, "node_id": node["node_id"]},
                    start_to_close_timeout=timedelta(seconds=node["timeout_seconds"]),
                    # Workflows started before this field existed are still replayable.
                    schedule_to_close_timeout=timedelta(
                        seconds=node.get("schedule_to_close_seconds")
                        or _node_schedule_to_close_seconds(node["node_id"])
                    ),
                    heartbeat_timeout=timedelta(seconds=NODE_HEARTBEAT_TIMEOUT_SECONDS),
                    retry_policy=_retry_policy(node["retry_policy"]),
                    cancellation_type=cancellation_type,
                )
                self.current_activity = activity_handle
                try:
                    result = await activity_handle
                except asyncio.CancelledError:
                    if self.cancel_requested:
                        return await self._cancel(run_id)
                    raise
                except ActivityError as exc:
                    if self.cancel_requested and isinstance(exc.__cause__, TemporalCancelledError):
                        return await self._cancel(run_id)
                    raise
                finally:
                    self.current_activity = None
                if result.get("cancelled"):
                    return await self._cancel(run_id)
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
                schedule_to_close_timeout=timedelta(minutes=15),
                retry_policy=TemporalRetryPolicy(maximum_attempts=5),
            )
            self.current_status = RunStatus.failed.value
            return {"run_id": run_id, "status": self.current_status}

    @workflow.signal(name="cancel")
    async def cancel(self, payload: dict[str, Any] | None = None) -> None:
        self.cancel_requested = True
        requested_mode = "force" if (payload or {}).get("mode") == "force" else "graceful"
        if self.cancel_mode != "force":
            self.cancel_mode = requested_mode
        self.current_status = RunStatus.cancelling.value
        if self.current_activity is not None:
            self.current_activity.cancel()

    @workflow.query(name="status")
    def status(self) -> str:
        return self.current_status

    async def _cancel(self, run_id: str) -> dict[str, Any]:
        result = await workflow.execute_activity(
            "mark_run_cancelled",
            {"run_id": run_id},
            start_to_close_timeout=timedelta(minutes=2),
            schedule_to_close_timeout=timedelta(minutes=5),
        )
        self.current_status = RunStatus.cancelled.value
        return {"run_id": run_id, "status": result["run_status"]}


def _admission_summary_is_idle(summary: dict[str, Any]) -> bool:
    """True when the case has no queued (admitted) and no active (running) runs.

    This is the admission controller's terminal condition: nothing is waiting and
    nothing is in flight, so the workflow can end. Deterministic (pure dict reads),
    so it is safe to evaluate inside the workflow body.
    """
    return (
        int(summary.get("queued_remaining") or 0) == 0
        and int(summary.get("active_count") or 0) == 0
    )


@workflow.defn(name=CASE_ADMISSION_WORKFLOW_TYPE)
class CaseRunAdmissionWorkflow:
    """Per-case FIFO admission controller for long-running batch production."""

    def __init__(self) -> None:
        self.poke_requested = False
        self.current_summary: dict[str, Any] = {}

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        case_id = str(payload["case_id"])
        cycles = 0
        while True:
            self.poke_requested = False
            self.current_summary = await workflow.execute_activity(
                "admit_case_runs",
                {"case_id": case_id},
                start_to_close_timeout=timedelta(minutes=5),
                schedule_to_close_timeout=timedelta(minutes=20),
                retry_policy=TemporalRetryPolicy(maximum_attempts=3),
            )
            cycles += 1
            # Idle exit: when the admit activity reports the case has no queued
            # (admitted) and no active (running) runs, this controller has nothing
            # left to do and ends instead of polling forever. Exit-race invariant:
            # the next batch's submitter always re-drives admission via
            # ``signal_case_admission_with_client`` (start-or-signal). Once this
            # workflow is terminal, ``start_workflow`` spins up a fresh instance, so
            # no admission is dropped. The only gap — a poke that lands as a signal
            # on this run while it is mid-termination — is reconciled by the worker's
            # 30s ``_admission_recovery_loop``, which re-pokes every case that still
            # has admitted runs.
            if _admission_summary_is_idle(self.current_summary):
                return dict(self.current_summary)
            if cycles >= CASE_ADMISSION_CONTINUE_AS_NEW_CYCLES:
                workflow.continue_as_new({"case_id": case_id})
            await workflow.wait_condition(
                lambda: self.poke_requested,
                timeout=timedelta(seconds=CASE_ADMISSION_POLL_SECONDS),
            )

    @workflow.signal(name="poke")
    async def poke(self, payload: dict[str, Any] | None = None) -> None:
        self.poke_requested = True

    @workflow.query(name="summary")
    def summary(self) -> dict[str, Any]:
        return dict(self.current_summary)


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
    except TemporalCancelledError:
        raise
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


class _TemporalCancellationToken:
    def __init__(
        self,
        run_id: str,
        production_repository: SqlAlchemyProductionRepository | None,
    ) -> None:
        self.run_id = run_id
        self.production_repository = production_repository
        self._mode: str | None = None
        self._next_db_poll = 0.0

    def _refresh_mode(self) -> None:
        if self.production_repository is None:
            return
        now = time.monotonic()
        if now < self._next_db_poll:
            return
        self._mode = self.production_repository.requested_run_cancel_mode(self.run_id)
        self._next_db_poll = now + CANCELLATION_DB_POLL_SECONDS

    @property
    def cancelled(self) -> bool:
        self._refresh_mode()
        return activity.is_cancelled() or self._mode is not None

    @property
    def force(self) -> bool:
        self._refresh_mode()
        return self._mode == "force"


@activity.defn(name="run_node")
def run_node(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    run_id = str(payload["run_id"])
    node_id = str(payload["node_id"])
    repository, runtime = _activity_runtime(ctx)
    if ctx.production_repository is not None:
        # The workflow task may dispatch its first activity before the admission
        # activity finishes its post-start status write. Make the activity-side
        # transition idempotent so a live child process is never hidden behind
        # durable ``admitted``; a cancellation that won the row lock remains final.
        ctx.production_repository.mark_run_started(run_id)
        ctx.production_repository.hydrate_workflow_runtime_snapshot(repository, run_id)
    token = _bind_activity_context(repository, run_id, node_id=node_id)
    activity.heartbeat({"run_id": run_id, "node_id": node_id, "phase": "started"})
    stop_heartbeat = _start_node_heartbeat(run_id, node_id)
    try:
        cancellation_token = _TemporalCancellationToken(run_id, ctx.production_repository)
        with cancellation_scope(cancellation_token), activity.shield_thread_cancel_exception():
            summary = runtime.run_node_activity(run_id, node_id)
            _sync_if_configured(ctx, repository, run_id)
            activity.heartbeat({"run_id": run_id, "node_id": node_id, "phase": "finished"})
            return summary
    except ExecutionCancelled:
        # The SQL fence can reach this thread before the SDK delivers its
        # Activity cancel task. A normal cleanup acknowledgement is still safe:
        # the Workflow signal owns the terminal transition and will call
        # ``mark_run_cancelled`` only after this Activity has returned.
        return {
            "run_id": run_id,
            "node_id": node_id,
            "node_status": "cancelled",
            "run_status": RunStatus.cancelling.value,
            "cancelled": True,
        }
    except TemporalCancelledError:
        raise
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


@activity.defn(name="admit_case_runs")
def admit_case_runs(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = _context()
    case_id = str(payload["case_id"])
    if ctx.production_repository is None:
        return {
            "case_id": case_id,
            "admitted_run_ids": [],
            "active_count": 0,
            "queued_remaining": 0,
        }
    settings = load_workflow_runtime_settings()
    summary = ctx.production_repository.admit_case_runs(
        case_id=case_id,
        max_inflight=settings.case_max_inflight_runs,
    )
    admitted = list(summary.get("admitted") or [])
    admitted_run_ids: list[str] = []
    start_error_run_ids: list[str] = []
    if admitted:
        # Start the whole admitted batch over ONE event loop and ONE Temporal
        # client. The previous per-run ``asyncio.run(Client.connect(...))`` opened
        # (and leaked) a fresh connection for every run.
        asyncio.run(
            _start_admitted_workflows(
                settings,
                ctx.production_repository,
                case_id=case_id,
                admitted=admitted,
                admitted_run_ids=admitted_run_ids,
                start_error_run_ids=start_error_run_ids,
            )
        )
    return {
        "case_id": case_id,
        "admitted_run_ids": admitted_run_ids,
        "start_error_run_ids": start_error_run_ids,
        "active_count": int(summary.get("active_count") or 0) + len(admitted_run_ids),
        "queued_remaining": max(
            0, int(summary.get("queued_remaining") or 0) - len(admitted_run_ids)
        ),
    }


async def _start_admitted_workflows(
    settings: WorkflowRuntimeSettings,
    production_repository: SqlAlchemyProductionRepository,
    *,
    case_id: str,
    admitted: list[tuple[Job, WorkflowRun]],
    admitted_run_ids: list[str],
    start_error_run_ids: list[str],
) -> None:
    """Start every admitted run through a single shared Temporal client.

    Fault isolation matches the original per-run semantics: a run whose start
    fails stays durably ``admitted`` (never ``mark_run_started``) so the next
    admission cycle retries it. A connect failure fails the whole batch the same
    way — every run is left admitted for retry — since no run could be started.
    """
    try:
        client = await asyncio.wait_for(
            Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
            ),
            timeout=TEMPORAL_CONNECT_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - unreachable Temporal leaves the batch retryable.
        logger.warning(
            "Temporal connect failed for admitted case runs; leaving batch admitted for retry",
            extra={"case_id": case_id, "run_ids": [run.id for _job, run in admitted]},
            exc_info=True,
        )
        start_error_run_ids.extend(run.id for _job, run in admitted)
        record_temporal_activity_failure()
        return
    for job, run in admitted:
        try:
            await _start_admitted_workflow(settings, client, job=job, run=run)
            production_repository.mark_run_started(run.id)
            admitted_run_ids.append(run.id)
        except Exception:  # noqa: BLE001 - keep admitted rows retryable on transient start errors.
            logger.warning(
                "Temporal start failed for admitted case run; leaving run admitted for retry",
                extra={"run_id": run.id, "case_id": case_id},
                exc_info=True,
            )
            start_error_run_ids.append(run.id)
            record_temporal_activity_failure()


async def _start_admitted_workflow(
    settings: WorkflowRuntimeSettings, client: Client, *, job: Job, run: WorkflowRun
) -> None:
    try:
        await client.start_workflow(
            WORKFLOW_TYPE,
            _workflow_payload(job=job, run=run, template=_template_from_run(run), reuse_plan=None),
            id=run.id,
            task_queue=settings.temporal_task_queue,
            rpc_timeout=TEMPORAL_RPC_TIMEOUT,
        )
    except WorkflowAlreadyStartedError:
        return


async def signal_case_admission_with_client(
    client: Client,
    settings: WorkflowRuntimeSettings,
    case_id: str,
) -> None:
    workflow_id = f"{CASE_ADMISSION_WORKFLOW_ID_PREFIX}{case_id}"
    try:
        await client.start_workflow(
            CASE_ADMISSION_WORKFLOW_TYPE,
            {"case_id": case_id},
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
            rpc_timeout=TEMPORAL_RPC_TIMEOUT,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("poke", {"case_id": case_id}, rpc_timeout=TEMPORAL_RPC_TIMEOUT)


def _bind_activity_context(repository: Repository, run_id: str, node_id: str | None = None):
    run = repository.runs.get(run_id)
    return bind_observability_context(
        job_id=run.job_id if run is not None else None,
        run_id=run_id,
        node_id=node_id,
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
        production_repository: SqlAlchemyProductionRepository | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.production_repository = production_repository
        # A single connected client is reused across requests on a dedicated
        # background event loop. temporalio's Client is bound to the loop it was
        # connected on, so we own a persistent loop instead of spinning up a new
        # one (and a new connection) per call. ``close()`` tears both down.
        self._client_obj: Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start_run(self, *, job: Job, run: WorkflowRun, template: WorkflowTemplate) -> None:
        self._run(
            self._start_workflow(
                _workflow_payload(job=job, run=run, template=template, reuse_plan=None)
            )
        )

    def signal_case_admission(self, case_id: str) -> None:
        self._run(self._signal_case_admission(case_id))

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
        previous = self.repository.runs.get(run_id) if self.repository is not None else None
        signal_force = force
        if self.production_repository is not None:
            requested = self.production_repository.request_run_cancellation(
                run_id,
                force=force,
            )
            if self.repository is not None:
                self.repository.runs[run_id] = requested
            if requested.status in {
                RunStatus.succeeded,
                RunStatus.failed,
                RunStatus.cancelled,
            }:
                return requested
            signal_force = self.production_repository.run_cancel_mode(run_id) == "force"
        else:
            if previous is not None and previous.status in {
                RunStatus.succeeded,
                RunStatus.failed,
                RunStatus.cancelled,
            }:
                return previous
            self._mark_local_cancelling(run_id)
        self._run(self._cancel_workflow(run_id, force=signal_force, reason=reason))
        return self.repository.runs.get(run_id) if self.repository is not None else None

    async def _client(self) -> Client:
        if self._client_obj is not None:
            return self._client_obj
        try:
            client = await asyncio.wait_for(
                Client.connect(
                    self.settings.temporal_address,
                    namespace=self.settings.temporal_namespace,
                ),
                timeout=TEMPORAL_CONNECT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError, RPCError, OSError) as exc:
            raise NodeExecutionError(
                ErrorCode.workflow_worker_lost,
                f"Cannot reach Temporal at {self.settings.temporal_address}: {exc}",
            ) from exc
        self._client_obj = client
        return client

    async def _start_workflow(self, payload: dict[str, Any]) -> None:
        client = await self._client()
        try:
            await client.start_workflow(
                WORKFLOW_TYPE,
                payload,
                id=str(payload["run_id"]),
                task_queue=self.settings.temporal_task_queue,
                rpc_timeout=TEMPORAL_RPC_TIMEOUT,
            )
        except WorkflowAlreadyStartedError:
            # Idempotent retry: a workflow with this run_id already exists (the
            # first request succeeded at the Temporal side even if the client
            # never saw the response). Treat as success and let the worker drive
            # the run forward — do NOT fail/recreate it.
            return

    async def _signal_case_admission(self, case_id: str) -> None:
        client = await self._client()
        await signal_case_admission_with_client(client, self.settings, case_id)

    async def _cancel_workflow(self, run_id: str, *, force: bool, reason: str | None) -> None:
        client = await self._client()
        handle = client.get_workflow_handle(run_id)
        await handle.signal(
            "cancel",
            {
                "mode": "force" if force else "graceful",
                "reason": reason or "",
            },
            rpc_timeout=TEMPORAL_RPC_TIMEOUT,
        )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run_loop_forever,
                args=(loop,),
                name="temporal-control-plane-loop",
                daemon=True,
            )
            thread.start()
            self._loop = loop
            self._loop_thread = thread
            return loop

    @staticmethod
    def _run_loop_forever(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _run(self, coroutine):
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=TEMPORAL_CALL_TIMEOUT_SECONDS)
        except FuturesTimeoutError as exc:
            future.cancel()
            raise NodeExecutionError(
                ErrorCode.workflow_worker_lost,
                f"Temporal request timed out after {TEMPORAL_CALL_TIMEOUT_SECONDS}s.",
            ) from exc
        except RPCError as exc:
            raise NodeExecutionError(
                ErrorCode.workflow_worker_lost, f"Temporal RPC failed: {exc}"
            ) from exc

    def close(self) -> None:
        """Stop the background loop and drop the cached client.

        Wired into the API lifespan shutdown. Idempotent and safe to call even
        if no loop/client was ever created.
        """
        with self._lock:
            loop = self._loop
            thread = self._loop_thread
            self._loop = None
            self._loop_thread = None
            self._client_obj = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=5)

    def _mark_local_cancelling(self, run_id: str) -> None:
        if self.repository is None or run_id not in self.repository.runs:
            return
        from packages.core.contracts import utcnow

        run = self.repository.runs[run_id]
        if run.status in {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}:
            return
        now = utcnow()
        status = RunStatus.cancelling if run.status == RunStatus.running else RunStatus.cancelled
        self.repository.runs[run_id] = run.model_copy(
            update={
                "status": status,
                "finished_at": now if status == RunStatus.cancelled else None,
                "updated_at": now,
            }
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
                "schedule_to_close_seconds": _node_schedule_to_close_seconds(node.node_id),
            }
            for node in template.nodes
        ],
    }


def _template_from_run(run: WorkflowRun) -> WorkflowTemplate:
    from packages.production.pipeline.digital_human import template_for

    template = template_for(run.workflow_template_id)
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
    # Seedance video generation is an async vendor task (submit + poll for minutes);
    # give it ample headroom over the 30min default so the activity is not cut by
    # start_to_close before the provider's own poll budget surfaces a timeout.
    if node_id == "SeedanceGenerateVideo":
        return 60 * 60
    return 30 * 60


def _node_schedule_to_close_seconds(node_id: str) -> int:
    """Ceiling across ALL attempts of a node, so retries cannot stack unbounded.

    start_to_close bounds one attempt; without this a paid node that retries three
    times could hang for three times its (multi-hour) attempt budget before anything
    terminates it. Each value leaves room for the node's own attempts plus backoff.
    """
    if node_id == "LipSync":
        return 4 * 60 * 60
    if node_id == "SeedanceGenerateVideo":
        return 150 * 60
    return 90 * 60
