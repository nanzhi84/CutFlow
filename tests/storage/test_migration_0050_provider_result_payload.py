"""Regression for migration 0050 (issue #202).

Adds ``provider_invocations.result_payload`` (JSONB, nullable), the durable copy of a
succeeded provider call's result. There is deliberately no back-fill: rows that
succeeded before the column existed have no recoverable result, and the Gateway keeps
rejecting a re-run that hits one. Runs against real Postgres.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, select

from packages.core.contracts import ProviderStatus
from packages.core.storage.database import ProviderInvocationRow
from packages.core.storage.repository import new_id

MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0050_provider_result_payload.py")


def test_migration_revision_chains_to_the_prior_head():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0050_provider_result_payload"' in source
    assert 'down_revision = "0049_provider_idempotency"' in source
    # alembic version_num is VARCHAR(32); the id must fit.
    assert len("0050_provider_result_payload") <= 32


def test_result_payload_column_is_nullable_jsonb(db_session_factory):
    with db_session_factory() as session:
        columns = {
            column["name"]: column
            for column in inspect(session.connection()).get_columns("provider_invocations")
        }
        column = columns["result_payload"]
        assert column["type"].__class__.__name__ == "JSONB"
        assert column["nullable"] is True


def test_an_envelope_round_trips_and_a_legacy_row_stays_null(db_session_factory):
    envelope = {"result": {"output": {"audio_uri": "s3://b/a.wav"}}, "usage": {"id": "usage_1"}}
    with_payload = new_id("pinv")
    legacy = new_id("pinv")
    with db_session_factory() as session:
        for invocation_id, payload in ((with_payload, envelope), (legacy, None)):
            session.add(
                ProviderInvocationRow(
                    id=invocation_id,
                    provider_id="acme",
                    model_id="model",
                    provider_profile_id="profile_1",
                    capability_id="tts.speech",
                    status=ProviderStatus.succeeded.value,
                    result_payload=payload,
                )
            )
        session.commit()

    with db_session_factory() as session:
        assert session.get(ProviderInvocationRow, with_payload).result_payload == envelope
        assert session.get(ProviderInvocationRow, legacy).result_payload is None
        # The column is queryable as JSONB, which is what the ops reporting path needs.
        unreplayable = session.scalars(
            select(ProviderInvocationRow.id)
            .where(ProviderInvocationRow.status == ProviderStatus.succeeded.value)
            .where(ProviderInvocationRow.result_payload.is_(None))
        ).all()
        assert legacy in unreplayable
        assert with_payload not in unreplayable
