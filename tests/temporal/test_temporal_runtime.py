from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from temporalio.client import Client
from temporalio.worker import Worker

RUN_TEMPORAL_TESTS = os.getenv("CUTAGENT_RUN_TEMPORAL_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_TEMPORAL_TESTS,
    reason="Set CUTAGENT_RUN_TEMPORAL_TESTS=1 to run Temporal runtime integration tests.",
)

if RUN_TEMPORAL_TESTS:
    os.environ.setdefault("CUTAGENT_WORKFLOW_RUNTIME", "temporal")
    os.environ.setdefault("CUTAGENT_TEMPORAL_TASK_QUEUE", f"cutagent-test-{os.getpid()}")

from apps.api.main import app
from packages.ai.gateway import ProviderGateway, SqlAlchemyProviderRuntimeRepository
from packages.ai.prompts import PromptRegistry, SqlAlchemyPromptRuntimeRepository
from packages.core.storage import Repository
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import ArtifactRow, FinishedVideoRow, NodeRunRow, OutboxEventRow, WorkflowRunRow
from packages.core.storage.secret_store import LocalSecretStore
from packages.core.workflow import load_workflow_runtime_settings
from packages.core.observability.events import (
    InProcessFanoutHub,
    SqlAlchemyOutboxDispatcher,
)
from packages.core.workflow.temporal_adapter import (
    TemporalActivityContext,
    configure_temporal_activity_context,
    temporal_activities,
    temporal_workflows,
)
from packages.production import SqlAlchemyProductionRepository
from packages.production.pipeline import build_digital_human_workflow


def _session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy for Temporal integration tests.")
    return session_factory


class WorkerThread:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(timeout=20), "Temporal worker did not start."
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.loop is not None and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout=20)

    def _run(self) -> None:
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        settings = load_workflow_runtime_settings()
        session_factory = _session_factory()
        runtime_repository = Repository()
        secret_store = LocalSecretStore()
        provider_gateway = ProviderGateway(
            runtime_repository,
            provider_reader=SqlAlchemyProviderRuntimeRepository(session_factory),
            secret_store=secret_store,
        )
        prompt_registry = PromptRegistry(
            runtime_repository,
            prompt_reader=SqlAlchemyPromptRuntimeRepository(session_factory),
        )
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
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        async with Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=temporal_workflows(),
            activities=temporal_activities(),
            activity_executor=ThreadPoolExecutor(max_workers=8),
        ):
            self.ready.set()
            await self.stop_event.wait()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _payload(title: str) -> dict:
    return {
        "case_id": "case_demo",
        "title": title,
        "script": "用一个短脚本验证 Temporal worker 执行。",
        "voice": {"voice_id": "voice_sandbox"},
        "portrait": {"template_mode": "agent"},
        "broll": {"enabled": False},
        "bgm": {"enabled": False},
        "subtitle": {"enabled": True},
        "lipsync": {"enabled": True, "provider_profile_id": "runninghub.heygem.default"},
        "strictness": {"strict_timestamps": False},
    }


def _wait_for_status(session_factory, run_id: str, statuses: set[str], timeout_sec: int = 60) -> str:
    deadline = time.monotonic() + timeout_sec
    last = None
    while time.monotonic() < deadline:
        with session_factory() as session:
            row = session.get(WorkflowRunRow, run_id)
            last = row.status if row else None
            if last in statuses:
                return last
        time.sleep(0.25)
    raise AssertionError(f"Run {run_id} did not reach {statuses}; last status={last!r}")


def test_temporal_submit_worker_completes_and_persists_finished_video():
    session_factory = _session_factory()
    with WorkerThread(), TestClient(app) as client:
        _login(client)
        created = client.post("/api/jobs/digital-human-video", json=_payload("Temporal success"))
        assert created.status_code == 201, created.text
        run_id = created.json()["initial_run"]["id"]
        assert created.json()["initial_run"]["status"] == "admitted"

        assert _wait_for_status(session_factory, run_id, {"succeeded"}) == "succeeded"
        with session_factory() as session:
            assert session.scalar(select(FinishedVideoRow).where(FinishedVideoRow.run_id == run_id))


def test_temporal_cancel_before_worker_runs_finishes_cancelled_without_video():
    session_factory = _session_factory()
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/jobs/digital-human-video", json=_payload("Temporal cancel"))
        assert created.status_code == 201, created.text
        run_id = created.json()["initial_run"]["id"]
        cancelled = client.post(f"/api/runs/{run_id}/cancel", json={"reason": "test cancel"})
        assert cancelled.status_code == 202, cancelled.text

    with WorkerThread():
        assert _wait_for_status(session_factory, run_id, {"cancelled"}) == "cancelled"
    with session_factory() as session:
        assert session.scalar(select(FinishedVideoRow).where(FinishedVideoRow.run_id == run_id)) is None


def test_temporal_resume_reruns_from_missing_middle_artifact_file(tmp_path: Path):
    session_factory = _session_factory()
    with WorkerThread(), TestClient(app) as client:
        _login(client)
        created = client.post("/api/jobs/digital-human-video", json=_payload("Temporal resume source"))
        assert created.status_code == 201, created.text
        source_run_id = created.json()["initial_run"]["id"]
        assert _wait_for_status(session_factory, source_run_id, {"succeeded"}) == "succeeded"

        with session_factory() as session:
            middle_node = session.scalar(
                select(NodeRunRow)
                .where(NodeRunRow.run_id == source_run_id)
                .where(NodeRunRow.node_id == "NarrationAlignment")
            )
            assert middle_node is not None
            middle_artifact = session.get(ArtifactRow, middle_node.output_artifact_ids[0])
            assert middle_artifact is not None
            artifact_file = tmp_path / "missing-artifact.json"
            artifact_file.write_text("artifact", encoding="utf-8")
            middle_artifact.local_path = str(artifact_file)
            middle_artifact.uri = artifact_file.as_uri()
            middle_artifact.sha256 = "bad"
            session.commit()
            artifact_file.unlink()

        resumed = client.post(
            f"/api/runs/{source_run_id}/resume",
            json={"reason": "reuse valid prefix", "reuse_valid_artifacts": True},
        )
        assert resumed.status_code == 201, resumed.text
        new_run_id = resumed.json()["run"]["id"]
        assert _wait_for_status(session_factory, new_run_id, {"succeeded"}) == "succeeded"
        with session_factory() as session:
            skipped = [
                row.node_id
                for row in session.scalars(
                    select(NodeRunRow)
                    .where(NodeRunRow.run_id == new_run_id)
                    .order_by(NodeRunRow.created_at.asc())
                )
                if row.status == "skipped"
            ]
        assert skipped == ["ValidateRequest", "LoadCaseContext", "ResolveCreativeIntent", "TTS", "MaterialPackPlanning"]


def test_temporal_worker_outbox_events_reach_run_websocket(monkeypatch):
    # conftest disables the background dispatcher globally for deterministic
    # unit tests; this test exercises the real dispatcher -> WS path.
    monkeypatch.setenv("CUTAGENT_DISABLE_BACKGROUND_DISPATCHER", "0")
    session_factory = _session_factory()
    # Earlier tests in this suite run with the dispatcher disabled and leave a
    # pending backlog; drain it so this run's events are delivered promptly
    # (the global created_at,id dispatch order is head-of-line blocking).
    drain_dispatcher = SqlAlchemyOutboxDispatcher(
        session_factory=session_factory, hub=InProcessFanoutHub()
    )

    async def _drain() -> None:
        while await drain_dispatcher.dispatch_once():
            pass

    asyncio.run(_drain())
    with WorkerThread(), TestClient(app) as client:
        _login(client)
        created = client.post("/api/jobs/digital-human-video", json=_payload("Temporal websocket"))
        assert created.status_code == 201, created.text
        run_id = created.json()["initial_run"]["id"]
        token_response = client.get(f"/api/runs/{run_id}/events")
        assert token_response.status_code == 200, token_response.text
        token = token_response.json()["token"]

        with client.websocket_connect(f"/ws/runs/{run_id}?token={token}") as websocket:
            assert _wait_for_status(session_factory, run_id, {"succeeded"}) == "succeeded"
            anyio.run(client.app.state.outbox_dispatcher.dispatch_once)
            received = []
            for _ in range(30):
                message = websocket.receive_json()
                received.append(message)
                if message.get("event_type") == "node_update":
                    break

        assert any(message.get("event_type") == "node_update" for message in received)
        with session_factory() as session:
            assert session.scalar(
                select(OutboxEventRow)
                .where(OutboxEventRow.aggregate_id == run_id)
                .where(OutboxEventRow.topic == "workflow.node.updated")
            )
