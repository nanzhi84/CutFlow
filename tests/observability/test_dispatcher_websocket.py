from __future__ import annotations

from datetime import datetime, timezone

import anyio
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.core.storage.database import JobRow, OutboxEventRow, WorkflowRunRow


def _seed_run_in_sql(session_factory, *, run_id: str, job_id: str) -> None:
    """Create the minimal Job + WorkflowRun rows so ``/api/runs/{run_id}/events``
    passes its ``run_exists`` guard (the route now checks the SQL production repo)."""
    with session_factory() as session:
        session.add(
            JobRow(
                id=job_id,
                type="digital_human_video",
                status="running",
                case_id="case_demo",
                request_schema="DigitalHumanVideoRequest.v1",
                request={},
            )
        )
        session.flush()
        session.add(
            WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                case_id="case_demo",
                workflow_template_id="digital_human_v2",
                workflow_version="v1",
                status="running",
            )
        )
        session.commit()


def _write_sql_outbox_event(
    session_factory, *, topic: str, run_id: str, payload: dict, dedupe_key: str, created_at
) -> None:
    """Insert a pending RunEvent into the SQL outbox.

    Events flow through the SqlAlchemyOutboxDispatcher (live) + replay_sqlalchemy_outbox
    (history) now, not the in-memory repository.outbox, so the websocket fixture must
    seed Postgres directly."""
    with session_factory() as session:
        session.add(
            OutboxEventRow(
                id=payload["event_id"],
                topic=topic,
                aggregate_type="run",
                aggregate_id=run_id,
                dedupe_key=dedupe_key,
                payload_schema="RunEvent.v1",
                payload=payload,
                status="pending",
                available_at=created_at,
                created_at=created_at,
            )
        )
        session.commit()


def test_run_websocket_replays_history_and_receives_dispatched_events() -> None:
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert login.status_code == 200, login.text
        session_factory = client.app.state.sqlalchemy_session_factory
        _seed_run_in_sql(session_factory, run_id="run_ws", job_id="job_ws")
        _write_sql_outbox_event(
            session_factory,
            topic="workflow.run.updated",
            run_id="run_ws",
            payload={
                "event_id": "evt_history",
                "run_id": "run_ws",
                "job_id": "job_ws",
                "event_type": "run_update",
                "message": "history",
                "created_at": "2026-06-11T00:00:00+00:00",
            },
            dedupe_key="run_ws:history",
            created_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        )
        anyio.run(client.app.state.outbox_dispatcher.dispatch_once)

        token_response = client.get("/api/runs/run_ws/events")
        assert token_response.status_code == 200, token_response.text
        token_body = token_response.json()
        assert token_body["stream_url"] == "/ws/runs/run_ws"
        assert token_body["token"]

        with client.websocket_connect(f"/ws/runs/run_ws?token={token_body['token']}") as websocket:
            assert websocket.receive_json()["event_id"] == "evt_history"
            _write_sql_outbox_event(
                session_factory,
                topic="workflow.node.updated",
                run_id="run_ws",
                payload={
                    "event_id": "evt_node_a_running",
                    "run_id": "run_ws",
                    "job_id": "job_ws",
                    "event_type": "node_update",
                    "node_id": "NodeA",
                    "status": "running",
                    "message": "NodeA is running.",
                },
                dedupe_key="node_a:running",
                created_at=datetime(2026, 6, 11, 0, 0, 1, tzinfo=timezone.utc),
            )
            anyio.run(client.app.state.outbox_dispatcher.dispatch_once)
            live = websocket.receive_json()

        assert live["event_type"] == "node_update"
        assert live["node_id"] == "NodeA"
        assert live["status"] == "running"
