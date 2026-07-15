from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text

MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0056_media_prompt_domains.py")
SEED_PATH = Path("packages/core/storage/prompt_group_defaults.json")
REVISION = "0056_media_prompt_domains"
V1_VERSION_ID = "prompt_media_selection_agent_v1"
V2_VERSION_ID = "prompt_media_selection_agent_v2"
BINDING_ID = "prompt_binding_prompt_media_selection_agent"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0055", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v2_seed_content() -> str:
    payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return next(
        str(item["content"]) for item in payload["items"] if item["version_id"] == V2_VERSION_ID
    )


def _run_upgrade(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()


def test_migration_revision_preserves_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    migration = script.get_revision(REVISION)

    assert heads == ["0057_drop_provider_retry_policy"]
    assert migration is not None
    assert migration.down_revision == "0055_tts_async_icl2"
    assert len(REVISION) <= 32


def test_upgrade_publishes_v2_switches_binding_and_preserves_v1(
    db_session_factory,
) -> None:
    untouched_v1 = "published v1 history must stay byte-for-byte unchanged"
    with db_session_factory() as session:
        session.execute(
            text("update prompt_bindings set prompt_version_id = :v1 where id = :binding_id"),
            {"v1": V1_VERSION_ID, "binding_id": BINDING_ID},
        )
        session.execute(
            text("delete from prompt_versions where id = :v2"),
            {"v2": V2_VERSION_ID},
        )
        session.execute(
            text("update prompt_versions set content = :content where id = :v1"),
            {"content": untouched_v1, "v1": V1_VERSION_ID},
        )
        session.commit()

    _run_upgrade(db_session_factory)
    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        v1_content = conn.execute(
            text("select content from prompt_versions where id = :id"),
            {"id": V1_VERSION_ID},
        ).scalar_one()
        v2 = conn.execute(
            text("select content, status, changelog from prompt_versions where id = :id"),
            {"id": V2_VERSION_ID},
        ).one()
        binding = conn.execute(
            text("select prompt_version_id, node_id, enabled from prompt_bindings where id = :id"),
            {"id": BINDING_ID},
        ).one()
        version_count = conn.execute(
            text("select count(*) from prompt_versions where id = :id"),
            {"id": V2_VERSION_ID},
        ).scalar_one()

    assert v1_content == untouched_v1
    assert v2.content == _v2_seed_content()
    assert v2.status == "published"
    assert v2.changelog == "Publish slot-scoped, construction-safe media candidate domains."
    assert binding.prompt_version_id == V2_VERSION_ID
    assert binding.node_id == "MediaSelectionAgentPlanning"
    assert binding.enabled is True
    assert version_count == 1
