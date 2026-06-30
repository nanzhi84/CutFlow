from __future__ import annotations

import asyncio
import logging
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
from packages.core.storage import Repository
from packages.core.storage.bootstrap import (
    bootstrap_sqlalchemy_storage,
    get_sqlalchemy_session_factory,
)
from packages.core.observability import configure_logging
from packages.core.storage.secret_store import LocalSecretStore
from packages.core.storage.sqlalchemy_secrets import SqlAlchemySecretStore
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
    # Fail closed in production before connecting to anything (#66).
    _preflight_issues = validate_startup_settings(build_settings())
    if _preflight_issues:
        raise RuntimeError(format_preflight_report(_preflight_issues))
    bootstrap_sqlalchemy_storage()
    settings = load_workflow_runtime_settings()
    session_factory = get_sqlalchemy_session_factory()
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
    production_repository = SqlAlchemyProductionRepository(session_factory)
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
