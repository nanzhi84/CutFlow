from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from packages.ai.gateway import ProviderGateway, SqlAlchemyProviderRuntimeRepository
from packages.ai.prompts import PromptRegistry, SqlAlchemyPromptRuntimeRepository
from packages.core.storage import Repository
from packages.core.storage.bootstrap import (
    bootstrap_sqlalchemy_storage_if_enabled,
    get_sqlalchemy_session_factory_if_enabled,
)
from packages.core.observability import configure_logging
from packages.core.storage.secret_store import LocalSecretStore
from packages.core.workflow import load_workflow_runtime_settings
from packages.core.workflow.temporal_adapter import (
    TemporalActivityContext,
    configure_temporal_activity_context,
    temporal_activities,
    temporal_workflows,
)
from packages.ops import BudgetEnforcementGuard, SqlAlchemyOpsRepository
from packages.ops.circuit_breaker import ProviderCircuitBreaker
from packages.production import SqlAlchemyProductionRepository
from packages.production.pipeline import build_digital_human_workflow


async def async_main() -> None:
    configure_logging()
    bootstrap_sqlalchemy_storage_if_enabled()
    settings = load_workflow_runtime_settings()
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    runtime_repository = Repository()
    secret_store = LocalSecretStore()
    provider_reader = (
        SqlAlchemyProviderRuntimeRepository(session_factory) if session_factory is not None else None
    )
    prompt_reader = (
        SqlAlchemyPromptRuntimeRepository(session_factory) if session_factory is not None else None
    )
    ops_repository = SqlAlchemyOpsRepository(session_factory) if session_factory is not None else None
    provider_gateway = ProviderGateway(
        runtime_repository,
        provider_reader=provider_reader,
        secret_store=secret_store,
        budget_guard=BudgetEnforcementGuard(ops_repository) if ops_repository is not None else None,
        circuit_breaker=ProviderCircuitBreaker(session_factory) if session_factory is not None else None,
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
    production_repository = (
        SqlAlchemyProductionRepository(session_factory) if session_factory is not None else None
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
        activity_executor=ThreadPoolExecutor(max_workers=8),
    )
    logging.getLogger("cutagent.worker").info(
        "Cutagent Temporal worker ready: "
        f"{settings.temporal_namespace}/{settings.temporal_task_queue}",
        extra={
            "event": "worker_ready",
            "temporal_namespace": settings.temporal_namespace,
            "temporal_task_queue": settings.temporal_task_queue,
        },
    )
    await worker.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
