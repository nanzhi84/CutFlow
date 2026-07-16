from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from packages.ai.gateway import ProviderGateway, SqlAlchemyProviderRuntimeRepository
from packages.ai.prompts import PromptRegistry, SqlAlchemyPromptRuntimeRepository
from packages.core.config import (
    build_settings,
    format_preflight_report,
    validate_startup_settings,
)
from packages.core.storage import (
    Repository,
    configure_object_store,
    object_store_from_settings,
)
from packages.core.storage.bootstrap import (
    bootstrap_sqlalchemy_storage,
    get_sqlalchemy_session_factory,
)
from packages.core.observability import configure_logging
from packages.core.storage.secret_store import LocalSecretStore
from packages.core.storage.sqlalchemy_secrets import SqlAlchemySecretStore
from packages.core.storage.sqlalchemy_uploads import SqlAlchemyUploadRepository
from packages.core.workflow import load_workflow_runtime_settings
from packages.core.workflow.temporal_adapter import (
    CASE_ADMISSION_POLL_SECONDS,
    TEMPORAL_RPC_TIMEOUT,
    TemporalActivityContext,
    configure_temporal_activity_context,
    signal_case_admission_with_client,
    temporal_activities,
    temporal_workflows,
)
from packages.ops import BudgetEnforcementGuard, SqlAlchemyOpsRepository
from packages.ops.circuit_breaker import ProviderCircuitBreaker
from packages.production import SqlAlchemyProductionRepository
from packages.production.pipeline import build_digital_human_workflow
from packages.media import UploadReconciler


async def async_main() -> None:
    configure_logging()
    # Fail closed in production before connecting to anything (#66).
    app_settings = build_settings()
    _preflight_issues = validate_startup_settings(app_settings)
    if _preflight_issues:
        raise RuntimeError(format_preflight_report(_preflight_issues))
    bootstrap_sqlalchemy_storage()
    settings = load_workflow_runtime_settings()
    session_factory = get_sqlalchemy_session_factory()
    object_store = object_store_from_settings(
        app_settings.object_store,
        workflow_runtime=app_settings.workflow.runtime,
        redis_url=app_settings.redis_url,
    )
    configure_object_store(object_store)
    runtime_repository = Repository()
    local_secret_store = LocalSecretStore()
    secret_store = SqlAlchemySecretStore(session_factory, fallback=local_secret_store)
    provider_reader = SqlAlchemyProviderRuntimeRepository(session_factory)
    prompt_reader = SqlAlchemyPromptRuntimeRepository(session_factory)
    ops_repository = SqlAlchemyOpsRepository(session_factory)
    provider_gateway = ProviderGateway(
        runtime_repository,
        provider_reader=provider_reader,
        secret_store=secret_store,
        budget_guard=BudgetEnforcementGuard(ops_repository),
        circuit_breaker=ProviderCircuitBreaker(session_factory),
    )
    prompt_registry = PromptRegistry(runtime_repository, prompt_reader=prompt_reader)
    # Under the SQL backend this worker-global runtime is only a stateless-service
    # template (provider plugins/readers, secret/object stores, prompt reader): each
    # Temporal activity builds a FRESH, isolated Repository via
    # TemporalActivityContext.build_runtime() so concurrent runs never share mutable
    # run-state. It still seeds demo media once here so per-activity runtimes can
    # skip that expensive bootstrap.
    local_runtime = build_digital_human_workflow(
        runtime_repository,
        provider_gateway=provider_gateway,
        prompt_registry=prompt_registry,
    )
    production_repository = SqlAlchemyProductionRepository(session_factory, object_store)
    upload_reconciler = UploadReconciler(
        SqlAlchemyUploadRepository(session_factory),
        object_store,
        app_settings.upload,
        owner="temporal-worker",
    )
    configure_temporal_activity_context(
        TemporalActivityContext(
            repository=runtime_repository,
            local_runtime=local_runtime,
            production_repository=production_repository,
        )
    )
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=temporal_workflows(),
        activities=temporal_activities(),
        activity_executor=ThreadPoolExecutor(max_workers=settings.worker_max_activities),
    )
    logging.getLogger("cutagent.worker").info(
        "Cutagent Temporal worker ready: "
        f"{settings.temporal_namespace}/{settings.temporal_task_queue}",
        extra={
            "event": "worker_ready",
            "temporal_namespace": settings.temporal_namespace,
            "temporal_task_queue": settings.temporal_task_queue,
            "worker_max_activities": settings.worker_max_activities,
        },
    )
    admission_recovery_task = asyncio.create_task(
        _admission_recovery_loop(client, settings, production_repository)
    )
    upload_recovery_task = asyncio.create_task(
        _upload_recovery_loop(upload_reconciler, app_settings.upload.reconcile_interval_seconds)
    )
    try:
        await worker.run()
    finally:
        admission_recovery_task.cancel()
        upload_recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await admission_recovery_task
        with suppress(asyncio.CancelledError):
            await upload_recovery_task


async def _admission_recovery_loop(
    client: Client,
    settings,
    production_repository: SqlAlchemyProductionRepository,
) -> None:
    logger = logging.getLogger("cutagent.worker")
    while True:
        try:
            for case_id in production_repository.case_ids_with_admitted_runs(limit=100):
                try:
                    await signal_case_admission_with_client(client, settings, case_id)
                except Exception:
                    logger.warning(
                        "Failed to recover case admission controller",
                        extra={"event": "case_admission_recovery_failed", "case_id": case_id},
                        exc_info=True,
                    )
            await _reconcile_cancelling_runs(client, production_repository)
        except Exception:
            logger.warning(
                "Failed to scan workflow recovery state",
                extra={"event": "workflow_recovery_scan_failed"},
                exc_info=True,
            )
        await asyncio.sleep(float(CASE_ADMISSION_POLL_SECONDS))


async def _upload_recovery_loop(
    reconciler: UploadReconciler,
    interval_seconds: int,
) -> None:
    logger = logging.getLogger("cutagent.worker")
    while True:
        try:
            await asyncio.to_thread(reconciler.reconcile_once)
        except Exception:
            logger.warning(
                "Failed to reconcile interrupted uploads",
                extra={"event": "upload_reconcile_scan_failed"},
                exc_info=True,
            )
        await asyncio.sleep(float(interval_seconds))


async def _reconcile_cancelling_runs(
    client: Client,
    production_repository: SqlAlchemyProductionRepository,
) -> None:
    logger = logging.getLogger("cutagent.worker")
    for run_id in production_repository.run_ids_with_cancelling(limit=100):
        try:
            handle = client.get_workflow_handle(run_id)
            await handle.signal(
                "cancel",
                {
                    "mode": production_repository.run_cancel_mode(run_id),
                    "reason": "worker cancellation reconciliation",
                },
                rpc_timeout=TEMPORAL_RPC_TIMEOUT,
            )
        except Exception:
            logger.warning(
                "Failed to reconcile cancelling workflow",
                extra={
                    "event": "workflow_cancellation_recovery_failed",
                    "run_id": run_id,
                },
                exc_info=True,
            )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
