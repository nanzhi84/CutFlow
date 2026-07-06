from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from apps.api.dependencies import (
    authenticate_api_request,
    http_exception_handler,
    node_error_handler,
    request_validation_error_handler,
)
from apps.api.routers import (
    auth,
    case_agent,
    case_rubric,
    cases,
    creative,
    core,
    finished_videos,
    imports,
    jobs_runs,
    media,
    ops,
    prompts,
    providers,
    publish_accounts,
    publishing,
    secrets,
    uploads,
    voices,
)
from packages.ai.gateway import ProviderGateway, SqlAlchemyProviderRepository, SqlAlchemyProviderRuntimeRepository
from packages.ai.prompts import PromptRegistry, SqlAlchemyPromptRepository, SqlAlchemyPromptRuntimeRepository
from packages.core.auth import create_sqlalchemy_auth_service
from packages.core.config import (
    build_settings,
    format_preflight_report,
    validate_startup_settings,
)
from packages.core.observability import (
    EventStreamTokenStore,
    InProcessFanoutHub,
    SqlAlchemyOutboxDispatcher,
    configure_logging,
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
from packages.core.storage.secret_store import LocalSecretStore
from packages.core.storage.sqlalchemy_idempotency import SqlAlchemyIdempotencyRepository
from packages.core.storage.sqlalchemy_secrets import SqlAlchemySecretRepository, SqlAlchemySecretStore
from packages.core.storage.sqlalchemy_uploads import SqlAlchemyUploadRepository
from packages.core.workflow import NodeExecutionError, load_workflow_runtime_settings
from packages.creative.cases import (
    SqlAlchemyCaseLearningRepository,
    SqlAlchemyCaseRepository,
    SqlAlchemyCaseRubricRepository,
)
from packages.media import SqlAlchemyMediaRepository
from packages.ops import BudgetEnforcementGuard, SqlAlchemyOpsRepository
from packages.ops.circuit_breaker import ProviderCircuitBreaker
from packages.production import SqlAlchemyProductionRepository
from packages.production.pipeline import build_digital_human_workflow
from packages.publishing import SqlAlchemyAccountsRepository, SqlAlchemyPublishingRepository
from packages.publishing.connectors.xiaovmao_cdp import XiaoVmaoLoginManager

ROUTER_MODULES = (
    core,
    auth,
    uploads,
    secrets,
    cases,
    creative,
    jobs_runs,
    media,
    voices,
    prompts,
    providers,
    case_agent,
    case_rubric,
    finished_videos,
    publish_accounts,
    publishing,
    ops,
    imports,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed in production BEFORE any startup side effect. bootstrap_* seeds
    # the local admin/viewer with dev-default credentials when seed_local_auth is
    # on, so it must run AFTER the gate, never before: a misconfigured prod deploy
    # (open registration, local-dev salt, seeded admin, local object store, missing
    # Redis, ...) must NEITHER seed NOR start serving. Mirror the worker's order —
    # preflight on a fresh settings snapshot, fail closed, and only then bootstrap.
    # Aggregates every unsafe setting into one error (#66).
    _preflight_issues = validate_startup_settings(build_settings())
    if _preflight_issues:
        raise RuntimeError(format_preflight_report(_preflight_issues))
    bootstrap_sqlalchemy_storage()
    session_factory = get_sqlalchemy_session_factory()
    configure_app_state(app, session_factory=session_factory)
    from apps.api.services.clip_embeddings import reconcile_interrupted_clip_embedding_jobs

    reconcile_interrupted_clip_embedding_jobs(app)
    dispatcher_task = None
    if not app.state.settings.api.disable_background_dispatcher:
        dispatcher_task = asyncio.create_task(app.state.outbox_dispatcher.run())
    from apps.api.services.providers import build_balance_poller_service

    balance_poller = build_balance_poller_service(app)
    app.state.balance_poller = balance_poller
    await balance_poller.start()  # no-op unless settings.balance.poller_enabled
    try:
        yield
    finally:
        await balance_poller.stop()
        app.state.outbox_dispatcher.stop()
        if dispatcher_task is not None:
            dispatcher_task.cancel()
            try:
                await dispatcher_task
            except asyncio.CancelledError:
                pass
        close_hub = getattr(app.state.event_hub, "close", None)
        if close_hub is not None:
            close_hub()
        # Tear down the Temporal control-plane background loop + cached client
        # (no-op for the local runtime / test adapters that lack ``close``).
        close_workflow = getattr(app.state.workflow, "close", None)
        if close_workflow is not None:
            close_workflow()


def configure_app_state(app: FastAPI, *, session_factory=None) -> None:
    app.state.settings = build_settings()
    if session_factory is None:
        session_factory = get_sqlalchemy_session_factory()
    runtime_repository = Repository()
    app.state.repository = runtime_repository
    # Publishing-center QR login: 小V猫 CDP manager. Platform sessions live in
    # 小V猫, never in SecretStore/DB.
    app.state.xiaovmao_login_manager = XiaoVmaoLoginManager(
        host=app.state.settings.publishing.xiaovmao_cdp_host,
        port=app.state.settings.publishing.xiaovmao_cdp_port,
        auto_launch=app.state.settings.publishing.xiaovmao_auto_launch,
    )
    app.state.event_hub = InProcessFanoutHub(redis_url=app.state.settings.redis_url)
    app.state.event_tokens = EventStreamTokenStore(redis_url=app.state.settings.redis_url)
    app.state.sqlalchemy_session_factory = session_factory
    app.state.outbox_dispatcher = SqlAlchemyOutboxDispatcher(
        session_factory=session_factory,
        hub=app.state.event_hub,
    )
    # Build the object store explicitly from THIS app's Settings snapshot and
    # install it into the process slot (issue #64), so the gateway / SQL repos —
    # which fall back to get_object_store() when not handed a store — resolve the
    # same instance instead of an import-time-frozen one.
    app.state.object_store = object_store_from_settings(
        app.state.settings.object_store,
        workflow_runtime=app.state.settings.workflow.runtime,
    )
    configure_object_store(app.state.object_store)
    local_secret_store = LocalSecretStore()
    app.state.secret_store = SqlAlchemySecretStore(session_factory, fallback=local_secret_store)
    app.state.sqlalchemy_case_repository = SqlAlchemyCaseRepository(session_factory)
    app.state.sqlalchemy_case_learning_repository = SqlAlchemyCaseLearningRepository(session_factory)
    app.state.sqlalchemy_case_rubric_repository = SqlAlchemyCaseRubricRepository(session_factory)
    app.state.sqlalchemy_upload_repository = SqlAlchemyUploadRepository(session_factory)
    app.state.sqlalchemy_media_repository = SqlAlchemyMediaRepository(session_factory, app.state.object_store)
    app.state.sqlalchemy_prompt_repository = SqlAlchemyPromptRepository(session_factory)
    app.state.sqlalchemy_provider_repository = SqlAlchemyProviderRepository(session_factory)
    app.state.sqlalchemy_idempotency_repository = SqlAlchemyIdempotencyRepository(session_factory)
    app.state.sqlalchemy_secret_repository = SqlAlchemySecretRepository(session_factory, app.state.secret_store)
    app.state.sqlalchemy_ops_repository = SqlAlchemyOpsRepository(session_factory)
    app.state.sqlalchemy_publishing_repository = SqlAlchemyPublishingRepository(session_factory)
    app.state.sqlalchemy_accounts_repository = SqlAlchemyAccountsRepository(session_factory)
    app.state.sqlalchemy_production_repository = SqlAlchemyProductionRepository(session_factory, app.state.object_store)
    app.state.auth_service = create_sqlalchemy_auth_service(session_factory)
    provider_reader = SqlAlchemyProviderRuntimeRepository(session_factory)
    prompt_reader = SqlAlchemyPromptRuntimeRepository(session_factory)
    budget_guard = BudgetEnforcementGuard(app.state.sqlalchemy_ops_repository)
    app.state.provider_gateway = ProviderGateway(
        runtime_repository,
        provider_reader=provider_reader,
        secret_store=app.state.secret_store,
        budget_guard=budget_guard,
        circuit_breaker=ProviderCircuitBreaker(session_factory),
    )
    app.state.prompt_registry = PromptRegistry(runtime_repository, prompt_reader=prompt_reader)
    app.state.workflow_runtime_settings = load_workflow_runtime_settings()
    def _sync_local_workflow_snapshot(job, run, repository):
        if app.state.sqlalchemy_production_repository is None:
            return
        app.state.sqlalchemy_production_repository.sync_workflow_snapshot(
            job=job,
            run=run,
            repository=repository,
        )

    local_runtime = build_digital_human_workflow(
        runtime_repository,
        provider_gateway=app.state.provider_gateway,
        prompt_registry=app.state.prompt_registry,
        snapshot_sync=_sync_local_workflow_snapshot,
        production_repository=app.state.sqlalchemy_production_repository,
    )
    if app.state.workflow_runtime_settings.runtime == "temporal":
        from packages.core.workflow.temporal_adapter import TemporalRuntimeAdapter

        app.state.workflow = TemporalRuntimeAdapter(
            app.state.workflow_runtime_settings,
            repository=runtime_repository,
        )
    else:
        app.state.workflow = local_runtime


def create_app() -> FastAPI:
    configure_logging()
    # Fail closed BEFORE configure_app_state eagerly builds the SQLAlchemy engine:
    # a production deploy missing CUTAGENT_DATABASE_URL would otherwise hit the
    # low-level engine RuntimeError here (still fail-closed, but one opaque error)
    # instead of lifespan's aggregated preflight report. Mirror lifespan's gate so
    # eager app construction surfaces the full unsafe-config list. (#87 B4)
    _preflight_issues = validate_startup_settings(build_settings())
    if _preflight_issues:
        raise RuntimeError(format_preflight_report(_preflight_issues))
    app = FastAPI(
        title="Cutagent Clean-Slate API",
        version="0.1.0",
        description="Case-first digital human production API generated from the clean-slate spec.",
        lifespan=lifespan,
    )
    configure_app_state(app)
    app.add_exception_handler(NodeExecutionError, node_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.middleware("http")(authenticate_api_request)
    for router_module in ROUTER_MODULES:
        app.include_router(router_module.router)
    return app
