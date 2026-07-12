"""Regression for migration 0049 (issue #193).

Adds ``provider_invocations.idempotency_key`` plus a partial unique index
``UNIQUE (idempotency_key) WHERE idempotency_key IS NOT NULL``. This proves the
migration chains to the prior single head, that the column exists, and that the
partial index de-duplicates non-null keys while leaving multiple NULLs (the vast
majority of invocations) unconstrained. Runs against real Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from packages.core.storage.database import ProviderInvocationRow
from packages.core.storage.repository import new_id

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0049_provider_idempotency.py"
)


def test_migration_revision_chains_to_single_head():
    text_src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0049_provider_idempotency"' in text_src
    assert 'down_revision = "0048_emphasis_floor_prompts"' in text_src
    # alembic version_num column is VARCHAR(32); the id must fit.
    assert len("0049_provider_idempotency") <= 32


def test_idempotency_key_column_and_partial_index_present(db_session_factory):
    with db_session_factory() as session:
        inspector = inspect(session.connection())
        columns = {col["name"] for col in inspector.get_columns("provider_invocations")}
        assert "idempotency_key" in columns
        indexes = {idx["name"] for idx in inspector.get_indexes("provider_invocations")}
        assert "uq_provider_invocations_idempotency_key" in indexes


def _invocation(idempotency_key: str | None) -> ProviderInvocationRow:
    return ProviderInvocationRow(
        id=new_id("pinv"),
        idempotency_key=idempotency_key,
        provider_id="acme",
        model_id="model",
        provider_profile_id="profile_1",
        capability_id="tts.speech",
        status="prepared",
    )


def test_partial_unique_index_rejects_duplicate_non_null_key(db_session_factory):
    key = f"provider-call:v1:{new_id('k')}"
    with db_session_factory() as session:
        session.add(_invocation(key))
        session.commit()
    with db_session_factory() as session:
        session.add(_invocation(key))
        with pytest.raises(IntegrityError):
            session.commit()


def test_partial_unique_index_allows_multiple_null_keys(db_session_factory):
    with db_session_factory() as session:
        session.add(_invocation(None))
        session.add(_invocation(None))
        session.commit()
        count = session.execute(
            text("SELECT count(*) FROM provider_invocations WHERE idempotency_key IS NULL")
        ).scalar_one()
        assert count >= 2
