from __future__ import annotations


from datetime import timedelta

import anyio
import pytest
from sqlalchemy import select


from packages.core.contracts import utcnow
from packages.core.observability.events import (
    InProcessFanoutHub,
    SqlAlchemyOutboxDispatcher,
    replay_sqlalchemy_outbox,
)
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from packages.core.storage.database import OutboxEventRow


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def test_sqlalchemy_outbox_replay_and_dispatcher_are_stable_and_idempotent() -> None:
    session_factory = sqlalchemy_session_factory()
    run_id = "run_observability_sql"
    created_at = utcnow()
    rows = [
        OutboxEventRow(
            id="evt_sql_b",
            topic="workflow.node.updated",
            aggregate_type="run",
            aggregate_id=run_id,
            dedupe_key="node:b",
            payload_schema="RunEvent.v1",
            payload={"event_id": "evt_sql_b", "run_id": run_id, "job_id": "job_sql", "event_type": "node_update"},
            status="pending",
            attempts=0,
            available_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        ),
        OutboxEventRow(
            id="evt_sql_a",
            topic="workflow.run.updated",
            aggregate_type="run",
            aggregate_id=run_id,
            dedupe_key="run:a",
            payload_schema="RunEvent.v1",
            payload={"event_id": "evt_sql_a", "run_id": run_id, "job_id": "job_sql", "event_type": "run_update"},
            status="pending",
            attempts=0,
            available_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        ),
    ]
    with session_factory() as session:
        for row in rows:
            session.merge(row)
        session.commit()

    replayed = replay_sqlalchemy_outbox(session_factory, aggregate_type="run", aggregate_id=run_id)
    assert [event["event_id"] for event in replayed] == ["evt_sql_a", "evt_sql_b"]

    hub = InProcessFanoutHub()
    subscriber = hub.subscribe(run_id)
    dispatcher = SqlAlchemyOutboxDispatcher(session_factory=session_factory, hub=hub)

    # Drain the full pending backlog, not just one batch. Sibling integration tests
    # run with the dispatcher disabled and leave pending outbox rows; dispatch_once
    # claims globally ORDER BY created_at LIMIT batch_size, so on a re-run against a
    # non-rebootstrapped DB this test's freshly-timestamped rows can fall outside a
    # single 100-row window (head-of-line blocking). Looping until dispatch_once()
    # returns 0 guarantees our events publish regardless of backlog. Mirrors
    # tests/temporal/test_temporal_runtime.py.
    async def _drain() -> None:
        while await dispatcher.dispatch_once():
            pass

    anyio.run(_drain)

    assert [hub.get_nowait(subscriber)["event_id"], hub.get_nowait(subscriber)["event_id"]] == [
        "evt_sql_a",
        "evt_sql_b",
    ]
    with session_factory() as session:
        stored = list(
            session.scalars(
                select(OutboxEventRow)
                .where(OutboxEventRow.aggregate_id == run_id)
                .order_by(OutboxEventRow.created_at, OutboxEventRow.id)
            )
        )
    assert [row.status for row in stored] == ["published", "published"]
    assert [row.attempts for row in stored] == [1, 1]


def test_sqlalchemy_outbox_replay_resumes_after_cursor() -> None:
    """#87 D2: replay(after_event_id=...) returns only events strictly after the
    cursor row's (created_at, id) position; an unknown id falls back to a full
    replay (the client dedups against its already-seen ids)."""
    session_factory = sqlalchemy_session_factory()
    run_id = "run_cursor_sql"
    base = utcnow()
    rows = [
        OutboxEventRow(
            id=f"evt_{suffix}",
            topic="workflow.run.updated",
            aggregate_type="run",
            aggregate_id=run_id,
            dedupe_key=f"cursor:{suffix}",
            payload_schema="RunEvent.v1",
            payload={"event_id": f"evt_{suffix}", "run_id": run_id, "event_type": "run_update"},
            status="pending",
            attempts=0,
            available_at=base + timedelta(milliseconds=index),
            created_at=base + timedelta(milliseconds=index),
            updated_at=base + timedelta(milliseconds=index),
        )
        for index, suffix in enumerate(["c1", "c2", "c3"])
    ]
    with session_factory() as session:
        for row in rows:
            session.merge(row)
        session.commit()

    full = replay_sqlalchemy_outbox(session_factory, aggregate_type="run", aggregate_id=run_id)
    assert [event["event_id"] for event in full] == ["evt_c1", "evt_c2", "evt_c3"]

    after_first = replay_sqlalchemy_outbox(
        session_factory, aggregate_type="run", aggregate_id=run_id, after_event_id="evt_c1"
    )
    assert [event["event_id"] for event in after_first] == ["evt_c2", "evt_c3"]

    after_last = replay_sqlalchemy_outbox(
        session_factory, aggregate_type="run", aggregate_id=run_id, after_event_id="evt_c3"
    )
    assert after_last == []

    after_unknown = replay_sqlalchemy_outbox(
        session_factory, aggregate_type="run", aggregate_id=run_id, after_event_id="evt_missing"
    )
    assert [event["event_id"] for event in after_unknown] == ["evt_c1", "evt_c2", "evt_c3"]
