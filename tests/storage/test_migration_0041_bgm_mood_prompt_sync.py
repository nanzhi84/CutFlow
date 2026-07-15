from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0041_bgm_mood_prompt_sync.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0041", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_chains_to_single_head():
    text_src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0041_bgm_mood_prompt_sync"' in text_src
    assert 'down_revision = "0040_dashscope_llm_timeout"' in text_src
    assert len("0041_bgm_mood_prompt_sync") <= 32


def test_upgrade_syncs_bgm_mood_and_scene_hint_prompts(db_session_factory):
    engine = db_session_factory.kw["bind"]
    with engine.begin() as conn:
        # The current Clean-Slate seed intentionally removes EditingAgentPlanning.
        # Reconstruct only the historical version row needed to replay 0041.
        conn.execute(
            text(
                """
                insert into prompt_templates (
                    id, name, purpose, variables_schema_ref, output_schema_ref,
                    status, schema_version, created_at, updated_at
                ) values (
                    'prompt_editing_agent', 'Historical Editing Agent',
                    'prompt.editing.agent', '{}'::jsonb, '{}'::jsonb,
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
                    'prompt_editing_agent_v1', 'prompt_editing_agent',
                    'historical editing prompt', 'published', 'v1', now(), now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                update prompt_versions
                set content = 'legacy creative intent prompt'
                where id = 'prompt_creative_intent_v1'
                """
            )
        )
        conn.execute(
            text(
                """
                update prompt_versions
                set content = 'legacy window query prompt'
                where id = 'prompt_window_query_v1'
                """
            )
        )
        conn.execute(
            text(
                """
                update prompt_bindings
                set prompt_version_id = 'prompt_window_query_v1'
                where id = 'prompt_binding_global_intent'
                """
            )
        )
        conn.execute(
            text(
                """
                update prompt_bindings
                set prompt_version_id = 'prompt_editing_agent_v1'
                where id = 'prompt_binding_prompt_window_query'
                """
            )
        )
        conn.execute(
            text(
                """
                insert into prompt_bindings (
                    id, prompt_template_id, prompt_version_id, case_id, node_id,
                    provider_profile_id, priority, enabled, schema_version,
                    created_at, updated_at
                )
                values (
                    'prompt_binding_custom_intent', 'prompt_creative_intent',
                    'prompt_window_query_v1', 'case_demo', 'ResolveCreativeIntent',
                    'sandbox.llm.default', 0, true, 'v1', now(), now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                insert into prompt_bindings (
                    id, prompt_template_id, prompt_version_id, case_id, node_id,
                    provider_profile_id, priority, enabled, schema_version,
                    created_at, updated_at
                )
                values (
                    'prompt_binding_custom_window_query', 'prompt_window_query',
                    'prompt_editing_agent_v1', 'case_demo', 'WindowQueryPlanning',
                    'sandbox.llm.default', 0, true, 'v1', now(), now()
                )
                """
            )
        )

    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()

    with engine.connect() as conn:
        creative_content = conn.execute(
            text("select content from prompt_versions where id = 'prompt_creative_intent_v1'")
        ).scalar_one()
        window_content = conn.execute(
            text("select content from prompt_versions where id = 'prompt_window_query_v1'")
        ).scalar_one()
        bindings = dict(
            conn.execute(
                text(
                    """
                    select id, prompt_version_id
                    from prompt_bindings
                    where id in (
                        'prompt_binding_global_intent',
                        'prompt_binding_prompt_window_query',
                        'prompt_binding_custom_intent',
                        'prompt_binding_custom_window_query'
                    )
                    """
                )
            ).all()
        )

    assert "bgm_mood" in creative_content
    assert "沉稳 / 温暖 / 轻快 / 励志 / 高能 / 紧张 / 高级 / 俏皮" in creative_content
    assert "scene_hint" in window_content
    assert bindings["prompt_binding_global_intent"] == "prompt_creative_intent_v1"
    assert bindings["prompt_binding_prompt_window_query"] == "prompt_window_query_v1"
    assert bindings["prompt_binding_custom_intent"] == "prompt_window_query_v1"
    assert bindings["prompt_binding_custom_window_query"] == "prompt_editing_agent_v1"
