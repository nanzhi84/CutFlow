from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0043_fullcov_single_clip_prompt.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0043", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_chains_to_single_head():
    text_src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0043_fullcov_single_clip_prompt"' in text_src
    assert 'down_revision = "0042_edit_agent_fullcov_prompt"' in text_src
    assert len("0043_fullcov_single_clip_prompt") <= 32


def test_upgrade_syncs_full_coverage_single_clip_prompt(db_session_factory):
    engine = db_session_factory.kw["bind"]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                update prompt_versions
                set content = 'legacy full_coverage 窗口可用多条候选顺序拼接，'
                    '同一 slot 可以输出多条不同 candidate_id 以累计覆盖 required_seconds'
                where id = 'prompt_editing_agent_v1'
                """
            )
        )
        conn.execute(
            text(
                """
                update prompt_bindings
                set prompt_version_id = 'prompt_window_query_v1'
                where id = 'prompt_binding_prompt_editing_agent'
                """
            )
        )

    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()

    with engine.connect() as conn:
        content = conn.execute(
            text("select content from prompt_versions where id = 'prompt_editing_agent_v1'")
        ).scalar_one()
        binding_version = conn.execute(
            text(
                """
                select prompt_version_id
                from prompt_bindings
                where id = 'prompt_binding_prompt_editing_agent'
                """
            )
        ).scalar_one()

    assert "每个 B-roll slot 最多只能输出一条 candidate_id" in content
    assert "available_seconds >= required_seconds" in content
    assert "多条候选顺序拼接" not in content
    assert "累计覆盖 required_seconds" not in content
    assert binding_version == "prompt_editing_agent_v1"
