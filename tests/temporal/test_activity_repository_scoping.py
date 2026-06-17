"""Per-activity Repository scoping isolation tests (no Postgres/Temporal/OSS).

These tests cover the load-bearing fix: under the SQL/Temporal backend each
Temporal activity must build and use a FRESH in-memory Repository so concurrent
``run_node`` activities for different runs cannot interleave reads/writes on a
shared set of dicts (cross-run data bleed + unbounded memory growth).

Everything here uses in-memory ``Repository`` instances and fakes; no shared
external services are touched.
"""

from __future__ import annotations

import temporalio.activity as temporal_activity

from packages.ai.gateway import ProviderGateway
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    Job,
    JobStatus,
    JobType,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
    DigitalHumanVideoRequest,
)
from packages.core.storage import Repository
from packages.core.storage.repository import new_id
from packages.core.workflow import temporal_adapter
from packages.core.workflow.temporal_adapter import (
    TemporalActivityContext,
    mark_run_cancelled,
    run_node,
)
from packages.production.pipeline import build_digital_human_workflow


def _make_artifact(run_id: str, kind: ArtifactKind = ArtifactKind.creative_intent) -> Artifact:
    return Artifact(
        id=new_id("art"),
        kind=kind,
        run_id=run_id,
        payload_schema="Test.v1",
        payload={"run": run_id},
    )


def _make_node_run(run_id: str) -> NodeRun:
    return NodeRun(
        id=new_id("nr"),
        run_id=run_id,
        node_id="ValidateRequest",
        node_version="v1",
        status=NodeStatus.succeeded,
        input_manifest_hash="hash",
    )


def test_two_repositories_do_not_bleed_run_state() -> None:
    """Writes to run A's artifacts/node_runs must not appear in run B's repo."""
    repo_a = Repository()
    repo_b = Repository()

    art_a = _make_artifact("run_a")
    node_a = _make_node_run("run_a")
    repo_a.artifacts[art_a.id] = art_a
    repo_a.node_runs["run_a"] = [node_a]

    art_b = _make_artifact("run_b")
    node_b = _make_node_run("run_b")
    repo_b.artifacts[art_b.id] = art_b
    repo_b.node_runs["run_b"] = [node_b]

    # Run A's mutable state is invisible to run B and vice versa.
    assert art_a.id in repo_a.artifacts
    assert art_a.id not in repo_b.artifacts
    assert art_b.id not in repo_a.artifacts

    assert "run_a" in repo_a.node_runs
    assert "run_a" not in repo_b.node_runs
    assert "run_b" not in repo_a.node_runs

    # The dicts are distinct objects, not aliases of one process-global mapping.
    assert repo_a.artifacts is not repo_b.artifacts
    assert repo_a.node_runs is not repo_b.node_runs


def _template_runtime() -> object:
    """A worker-global runtime usable as the stateless-service template.

    ``auto_register_real_plugins=False`` keeps construction offline (no httpx).
    """
    template_repo = Repository()
    gateway = ProviderGateway(template_repo, auto_register_real_plugins=False)
    registry = PromptRegistry(template_repo)
    return build_digital_human_workflow(
        template_repo,
        provider_gateway=gateway,
        prompt_registry=registry,
        seed_media=False,
    )


def test_build_runtime_returns_fresh_isolated_repository() -> None:
    template = _template_runtime()
    ctx = TemporalActivityContext(
        repository=template.repository,
        local_runtime=template,
        production_repository=object(),  # presence flips scoping on
    )
    assert ctx.scoping_enabled() is True

    repo1, runtime1 = ctx.build_runtime()
    repo2, runtime2 = ctx.build_runtime()

    # Each activity gets its own Repository + runtime, distinct from the
    # worker-global template repository.
    assert repo1 is not repo2
    assert repo1 is not template.repository
    assert repo2 is not template.repository
    assert runtime1 is not runtime2
    assert runtime1.repository is repo1
    assert runtime2.repository is repo2

    # Stateless services are reused (not rebuilt) per activity.
    assert runtime1.provider_gateway.plugins == template.provider_gateway.plugins
    assert runtime1.prompt_registry.prompt_reader is template.prompt_registry.prompt_reader

    # A write into one activity's repository never reaches the other or the
    # template repository.
    art = _make_artifact("run_x")
    repo1.artifacts[art.id] = art
    assert art.id not in repo2.artifacts
    assert art.id not in template.repository.artifacts


def test_build_runtime_propagates_circuit_breaker_and_budget_guard() -> None:
    """Per-activity gateways must carry the worker-global circuit breaker AND
    budget guard.

    build_runtime() rebuilds a fresh ProviderGateway for every activity. If it
    forgets to thread an opt-in control through, that control is silently dead on
    the worker (Temporal) execution path — which is where the pipeline's real
    provider calls happen — even when the operator enabled it. Budget guard was
    already threaded; the circuit breaker was not.
    """
    template = _template_runtime()
    sentinel_breaker = object()
    sentinel_budget = object()
    template.provider_gateway.circuit_breaker = sentinel_breaker
    template.provider_gateway.budget_guard = sentinel_budget
    ctx = TemporalActivityContext(
        repository=template.repository,
        local_runtime=template,
        production_repository=object(),  # presence flips scoping on
    )

    _repo, runtime = ctx.build_runtime()

    assert runtime.provider_gateway.circuit_breaker is sentinel_breaker
    assert runtime.provider_gateway.budget_guard is sentinel_budget


def test_scoping_disabled_without_production_repository() -> None:
    template = _template_runtime()
    ctx = TemporalActivityContext(
        repository=template.repository,
        local_runtime=template,
        production_repository=None,
    )
    assert ctx.scoping_enabled() is False


class _FakeProductionRepository:
    """In-memory stand-in for the SQL production repository.

    Records, per call, the identity of the Repository handed to it so the test
    can prove activities use a fresh repository each time and never the shared
    worker-global one.
    """

    def __init__(self) -> None:
        self.hydrated_repo_ids: list[int] = []
        self.synced_repo_ids: list[int] = []
        self.synced_artifacts: dict[str, set[str]] = {}

    def hydrate_workflow_runtime_snapshot(self, repository: Repository, run_id: str) -> None:
        self.hydrated_repo_ids.append(id(repository))
        request = DigitalHumanVideoRequest(
            case_id="case_demo", title=run_id, script="脚本", voice={"voice_id": "voice_sandbox"}
        )
        job = Job(
            id=f"job_{run_id}",
            type=JobType.digital_human_video,
            status=JobStatus.running,
            case_id="case_demo",
            created_by="usr_admin",
            request_schema="DigitalHumanVideoRequest.v1",
            request=request,
        )
        run = WorkflowRun(
            id=run_id,
            job_id=job.id,
            case_id="case_demo",
            workflow_template_id="digital_human_video",
            workflow_version="v1",
            status=RunStatus.running,
            requested_by="usr_admin",
        )
        repository.jobs[job.id] = job
        repository.runs[run.id] = run

    def sync_workflow_snapshot(self, *, job: Job, run: WorkflowRun, repository: Repository) -> None:
        self.synced_repo_ids.append(id(repository))
        self.synced_artifacts[run.id] = {
            art.id for art in repository.artifacts.values() if art.run_id == run.id
        }


class _FakeRuntime:
    """Stand-in runtime that mutates the repository it is bound to."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def run_node_activity(self, run_id: str, node_id: str) -> dict:
        art = _make_artifact(run_id)
        self.repository.artifacts[art.id] = art
        self.repository.node_runs.setdefault(run_id, []).append(_make_node_run(run_id))
        return {"run_id": run_id, "node_id": node_id, "run_status": RunStatus.running.value}

    def request_cancel(self, run_id: str) -> WorkflowRun:
        run = self.repository.runs[run_id]
        cancelled = run.model_copy(update={"status": RunStatus.cancelled})
        self.repository.runs[run_id] = cancelled
        return cancelled


def _scoped_context(monkeypatch, production: _FakeProductionRepository) -> tuple[TemporalActivityContext, list[int]]:
    template = _template_runtime()
    ctx = TemporalActivityContext(
        repository=template.repository,
        local_runtime=template,
        production_repository=production,
    )
    built_repo_ids: list[int] = []
    # Hold strong references to every built repository for the lifetime of the
    # test. Without this, a freed repo's address can be recycled by CPython for
    # the next one, making id()-based identity checks flaky (~1/5 runs).
    built_repos: list[Repository] = []

    def fake_build_runtime() -> tuple[Repository, _FakeRuntime]:
        repo = Repository()
        built_repos.append(repo)
        built_repo_ids.append(id(repo))
        return repo, _FakeRuntime(repo)

    monkeypatch.setattr(ctx, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(temporal_adapter, "_activity_context", ctx)
    return ctx, built_repo_ids


def test_run_node_builds_fresh_repository_per_call(monkeypatch) -> None:
    monkeypatch.setattr(temporal_activity, "heartbeat", lambda *a, **k: None)
    production = _FakeProductionRepository()
    ctx, built_repo_ids = _scoped_context(monkeypatch, production)

    shared_repo_id = id(ctx.repository)

    run_node({"run_id": "run_a", "node_id": "ValidateRequest"})
    run_node({"run_id": "run_b", "node_id": "ValidateRequest"})

    # Two distinct fresh repositories were built, one per activity invocation.
    assert len(built_repo_ids) == 2
    assert built_repo_ids[0] != built_repo_ids[1]

    # Neither activity touched the shared worker-global repository.
    assert shared_repo_id not in built_repo_ids
    assert shared_repo_id not in production.hydrated_repo_ids
    assert shared_repo_id not in production.synced_repo_ids
    assert not ctx.repository.artifacts
    assert not ctx.repository.node_runs

    # Hydrate and sync both saw the same fresh repo within a call, and a
    # different one across calls.
    assert production.hydrated_repo_ids == built_repo_ids
    assert production.synced_repo_ids == built_repo_ids

    # Each run only persisted its own artifacts -> no cross-run bleed.
    assert len(production.synced_artifacts["run_a"]) == 1
    assert len(production.synced_artifacts["run_b"]) == 1
    assert production.synced_artifacts["run_a"].isdisjoint(production.synced_artifacts["run_b"])


def test_mark_run_cancelled_uses_fresh_repository(monkeypatch) -> None:
    production = _FakeProductionRepository()
    ctx, built_repo_ids = _scoped_context(monkeypatch, production)
    shared_repo_id = id(ctx.repository)

    result = mark_run_cancelled({"run_id": "run_a"})

    assert result == {"run_id": "run_a", "run_status": RunStatus.cancelled.value}
    assert len(built_repo_ids) == 1
    assert shared_repo_id not in built_repo_ids
    assert production.hydrated_repo_ids == built_repo_ids
    assert not ctx.repository.runs or ctx.repository.runs.get("run_a") is None
