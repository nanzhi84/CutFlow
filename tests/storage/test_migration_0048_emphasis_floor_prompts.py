from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0048_emphasis_floor_prompts.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0048", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(engine) -> None:
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()


def test_migration_revision_chains_to_single_head():
    src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0048_emphasis_floor_prompts"' in src
    assert 'down_revision = "0047_media_selection_diversity"' in src
    assert len("0048_emphasis_floor_prompts") <= 32


def test_upgrade_syncs_emphasis_candidates_and_current_postprocess_prompt(
    db_session_factory,
):
    engine = db_session_factory.kw["bind"]
    with engine.begin() as conn:
        _insert_historical_postprocess_prompt(conn)
        conn.execute(
            text(
                "update prompt_versions set content = 'legacy intent 最多 6 条 2 到 30 字' "
                "where id = 'prompt_creative_intent_v1'"
            )
        )
        conn.execute(
            text(
                "update prompt_versions set content = 'legacy postprocess 不确定时可少选，空数组有效' "
                "where id = 'prompt_postprocess_agent_v1'"
            )
        )

    _upgrade(engine)

    with engine.connect() as conn:
        creative = conn.execute(
            text("select content from prompt_versions where id = 'prompt_creative_intent_v1'")
        ).scalar_one()
        postprocess = conn.execute(
            text("select content from prompt_versions where id = 'prompt_postprocess_agent_v1'")
        ).scalar_one()

    assert "至少给出 6 条" in creative
    assert "2 到 10 个字" in creative
    # 0048 intentionally reads the current prompt-group source. After 0052 the
    # candidate floor remains upstream, while final count legality belongs locally.
    assert "本地求解器负责最终数量、时间冲突和 hero 上限" in postprocess
    assert "每个 event_id 都输出且只输出一条 caption_choice" in postprocess


def test_upgrade_is_idempotent_and_preserves_migrated_rows(db_session_factory):
    engine = db_session_factory.kw["bind"]
    # Rows that already carry the new marker must not be clobbered on re-run.
    sentinel_creative = "已迁移内容标记 至少给出 6 条 尾部"
    sentinel_postprocess = "已迁移内容标记 必须选择 5 到 8 个事件的 caption option 尾部"
    with engine.begin() as conn:
        _insert_historical_postprocess_prompt(conn)
        conn.execute(
            text("update prompt_versions set content = :c where id = 'prompt_creative_intent_v1'"),
            {"c": sentinel_creative},
        )
        conn.execute(
            text(
                "update prompt_versions set content = :c where id = 'prompt_postprocess_agent_v1'"
            ),
            {"c": sentinel_postprocess},
        )

    _upgrade(engine)

    with engine.connect() as conn:
        creative = conn.execute(
            text("select content from prompt_versions where id = 'prompt_creative_intent_v1'")
        ).scalar_one()
        postprocess = conn.execute(
            text("select content from prompt_versions where id = 'prompt_postprocess_agent_v1'")
        ).scalar_one()

    assert creative == sentinel_creative
    assert postprocess == sentinel_postprocess


def _insert_historical_postprocess_prompt(conn) -> None:
    """Restore only the rows that existed when 0048 originally ran."""

    conn.execute(
        text(
            """
            insert into prompt_templates (
                id, name, purpose, variables_schema_ref, output_schema_ref,
                status, schema_version, created_at, updated_at
            ) values (
                'prompt_postprocess_agent', 'Historical PostProcess Agent',
                'prompt.postprocess.agent', '{}'::jsonb, '{}'::jsonb,
                'active', 'v1', now(), now()
            )
            """
        )
    )
    conn.execute(
        text(
            """
            insert into prompt_versions (
                id, prompt_template_id, content, status, schema_version,
                created_at, updated_at
            ) values (
                'prompt_postprocess_agent_v1', 'prompt_postprocess_agent',
                'historical postprocess prompt', 'published', 'v1', now(), now()
            )
            """
        )
    )
