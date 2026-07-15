from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0057_drop_provider_retry_policy.py")
REVISION = "0057_drop_provider_retry_policy"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0056", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()


def test_migration_revision_is_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    migration = script.get_revision(REVISION)

    assert script.get_heads() == [REVISION]
    assert migration is not None
    assert migration.down_revision == "0056_media_prompt_domains"
    assert len(REVISION) <= 32


def test_upgrade_drops_unused_retry_policy_column(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    with engine.begin() as conn:
        conn.execute(
            text(
                "alter table provider_profiles add column if not exists "
                "retry_policy jsonb not null default '{}'::jsonb"
            )
        )

    _run_upgrade(db_session_factory)
    _run_upgrade(db_session_factory)

    assert "retry_policy" not in {
        column["name"] for column in inspect(engine).get_columns("provider_profiles")
    }
