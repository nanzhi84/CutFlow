from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text

REVISION = "0064_caption_style_intensity"
_MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0064_caption_style_intensity.py")


def _upgrade(engine) -> None:
    spec = importlib.util.spec_from_file_location("_migration_0064", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            module.upgrade()


def test_migration_revision_is_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    migration = script.get_revision(REVISION)

    assert script.get_heads() == [REVISION]
    assert migration is not None
    assert migration.down_revision == "0063_workflow_cancel_request"
    assert len(REVISION) <= 32


def test_migration_publishes_intensity_prompt_contract(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "update prompt_bindings set prompt_version_id = 'prompt_creative_intent_v1' "
                "where id = 'prompt_binding_global_intent'"
            )
        )
        connection.execute(
            text("delete from prompt_versions where id = 'prompt_creative_intent_v2'")
        )
        connection.execute(
            text(
                "update prompt_versions set content = 'historical immutable prompt' "
                "where id = 'prompt_creative_intent_v1'"
            )
        )

    _upgrade(engine)
    _upgrade(engine)

    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                text(
                    "select id, content from prompt_versions "
                    "where id in ('prompt_creative_intent_v1', 'prompt_creative_intent_v2')"
                )
            ).all()
        )
        binding = connection.execute(
            text(
                "select prompt_version_id from prompt_bindings "
                "where id = 'prompt_binding_global_intent'"
            )
        ).scalar_one()

    assert rows["prompt_creative_intent_v1"] == "historical immutable prompt"
    assert "intensity" in rows["prompt_creative_intent_v2"]
    assert "hero 最多 1 个" in rows["prompt_creative_intent_v2"]
    assert "strong 最多 3 个" in rows["prompt_creative_intent_v2"]
    assert binding == "prompt_creative_intent_v2"
