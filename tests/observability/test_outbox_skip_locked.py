from __future__ import annotations

import json
import sqlite3

import anyio
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from packages.core.contracts import utcnow
from packages.core.observability.events import (
    InProcessFanoutHub,
    SqlAlchemyOutboxDispatcher,
)
from packages.core.storage.database import OutboxEventRow

sqlite3.register_adapter(list, json.dumps)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # pragma: no cover - registration side effect.
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):  # pragma: no cover - registration side effect.
    return "JSON"


def _sqlite_session_factory() -> sessionmaker:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OutboxEventRow.__table__.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_row(event_id: str, run_id: str, created_at) -> OutboxEventRow:
    return OutboxEventRow(
        id=event_id,
        topic="workflow.run.updated",
        aggregate_type="run",
        aggregate_id=run_id,
        dedupe_key=event_id,
        payload_schema="RunEvent.v1",
        payload={"event_id": event_id, "run_id": run_id, "job_id": "job_sl", "event_type": "run_update"},
        status="pending",
        attempts=0,
        available_at=created_at,
        created_at=created_at,
        updated_at=created_at,
    )


def test_claim_runs_under_sqlite_without_skip_locked_error() -> None:
    """SQLite lacks SKIP LOCKED: the dispatcher must still claim+publish without error."""
    session_factory = _sqlite_session_factory()
    run_id = "run_skip_locked"
    created_at = utcnow()
    with session_factory() as session:
        # Insert in REVERSE id order (same created_at) so the published order
        # asserted below can only come out [a, b] if the dispatcher's
        # ``ORDER BY (created_at, id)`` actually reorders — not if it merely
        # echoes insertion order. (#87 A2: preserves the stable-ordering
        # coverage of the deleted in-memory OutboxDispatcher test.)
        session.add(_make_row("evt_sl_b", run_id, created_at))
        session.add(_make_row("evt_sl_a", run_id, created_at))
        session.commit()

    hub = InProcessFanoutHub()
    subscriber = hub.subscribe(run_id)
    dispatcher = SqlAlchemyOutboxDispatcher(session_factory=session_factory, hub=hub)

    published = anyio.run(dispatcher.dispatch_once)

    assert published == 2
    assert hub.get_nowait(subscriber)["event_id"] == "evt_sl_a"
    assert hub.get_nowait(subscriber)["event_id"] == "evt_sl_b"
    with session_factory() as session:
        statuses = list(
            session.scalars(
                select(OutboxEventRow.status)
                .where(OutboxEventRow.aggregate_id == run_id)
                .order_by(OutboxEventRow.created_at, OutboxEventRow.id)
            )
        )
    assert statuses == ["published", "published"]
