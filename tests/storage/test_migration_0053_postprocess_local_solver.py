from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text

MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0053_postprocess_local_solver.py")
SEED_PATH = Path("packages/core/storage/prompt_group_defaults.json")
VERSION_ID = "prompt_postprocess_agent_v1"
LOCAL_SOLVER_MARKER = "本地求解器负责最终数量、时间冲突和 hero 上限"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0052", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_content() -> str:
    payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return next(
        str(item["content"]) for item in payload["items"] if item["version_id"] == VERSION_ID
    )


def _run_upgrade(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()


def test_migration_revision_chains_to_current_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    migration = script.get_revision("0053_postprocess_local_solver")

    assert heads == ["0057_drop_provider_retry_policy"]
    assert migration is not None
    assert migration.down_revision == "0052_finished_video_cover_thumb"
    assert len("0053_postprocess_local_solver") <= 32


def test_upgrade_syncs_postprocess_local_solver_contract(db_session_factory) -> None:
    with db_session_factory() as session:
        session.execute(
            text("update prompt_versions set content = :content where id = :id"),
            {"content": "legacy prompt: 必须选择 5 到 8 个花字", "id": VERSION_ID},
        )
        session.commit()

    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        content = conn.execute(
            text("select content from prompt_versions where id = :id"),
            {"id": VERSION_ID},
        ).scalar_one()

    assert content == _seed_content()
    assert LOCAL_SOLVER_MARKER in content
    assert "每个 event_id 都输出且只输出一条 caption_choice" in content


def test_upgrade_is_idempotent_and_preserves_marker_rows(db_session_factory) -> None:
    sentinel = f"自定义已迁移内容；{LOCAL_SOLVER_MARKER}；保留"
    with db_session_factory() as session:
        session.execute(
            text("update prompt_versions set content = :content where id = :id"),
            {"content": sentinel, "id": VERSION_ID},
        )
        session.commit()

    _run_upgrade(db_session_factory)
    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        content = conn.execute(
            text("select content from prompt_versions where id = :id"),
            {"id": VERSION_ID},
        ).scalar_one()

    assert content == sentinel
