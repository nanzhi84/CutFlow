from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0059_upload_normalized_state.py"
)
REVISION = "0059_upload_normalized_state"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0059", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(engine, fn) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            fn()


def test_migration_revision_is_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    migration = script.get_revision(REVISION)

    assert script.get_heads() == [REVISION]
    assert migration is not None
    assert migration.down_revision == "0058_resumable_uploads"
    assert len(REVISION) <= 32


def test_upgrade_adds_durable_normalized_state_and_is_idempotent(
    db_session_factory,
) -> None:
    engine = db_session_factory.kw["bind"]
    migration = _load_migration()

    _run(engine, migration.downgrade)
    assert "normalized" not in {
        column["name"] for column in inspect(engine).get_columns("upload_sessions")
    }

    _run(engine, migration.upgrade)
    _run(engine, migration.upgrade)
    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("upload_sessions")
    }
    normalized = columns["normalized"]
    assert normalized["nullable"] is False
    assert "false" in str(normalized["default"]).lower()
