"""Regression for the HuaziPlanningSubagent prompt migration (issue #188).

Caption Display v2 moved huazi planning out of the main editing agent into a
separate ``HuaziPlanningSubagent`` (a second LLM pass). Migration 0046 keeps
existing DBs correct on a migrate-only deploy path: it re-syncs the stored
``prompt_editing_agent_v1`` content to the huazi-free version and inserts the new
huazi subagent template / version / binding if they are missing.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from packages.core.storage.database import (
    PromptBindingRow,
    PromptTemplateRow,
    PromptVersionRow,
)

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0046_huazi_subagent_prompt.py"
)
_SEED_PATH = Path("packages/core/storage/prompt_group_defaults.json")


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0046", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_item(version_id: str) -> dict:
    payload = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    return next(item for item in payload["items"] if item["version_id"] == version_id)


def _run_upgrade(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()


def test_migration_revision_chains_to_single_head():
    text_src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0046_huazi_subagent_prompt"' in text_src
    assert 'down_revision = "0045_drop_subtitle_preset"' in text_src
    assert len("0046_huazi_subagent_prompt") <= 32


def test_upgrade_resyncs_legacy_editing_prompt_to_huazi_free(db_session_factory):
    with db_session_factory() as session:
        row = session.get(PromptVersionRow, "prompt_editing_agent_v1")
        row.content = "legacy {narration_units} with huazi_plan output block"
        session.commit()

    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        content = conn.execute(
            text("select content from prompt_versions where id = 'prompt_editing_agent_v1'")
        ).scalar_one()

    assert content == _seed_item("prompt_editing_agent_v1")["content"]
    assert "huazi_plan" not in content
    assert "{narration_units}" in content


def test_upgrade_inserts_huazi_subagent_when_missing(db_session_factory):
    # The reseed baseline already inserts the huazi rows; drop them to exercise the
    # migrate-only insert path, then assert the migration restores them.
    with db_session_factory() as session:
        session.query(PromptBindingRow).filter_by(id="prompt_binding_prompt_huazi_subagent").delete()
        session.query(PromptVersionRow).filter_by(id="prompt_huazi_subagent_v1").delete()
        session.query(PromptTemplateRow).filter_by(id="prompt_huazi_subagent").delete()
        session.commit()

    _run_upgrade(db_session_factory)

    seed_item = _seed_item("prompt_huazi_subagent_v1")
    with db_session_factory() as session:
        template = session.get(PromptTemplateRow, "prompt_huazi_subagent")
        version = session.get(PromptVersionRow, "prompt_huazi_subagent_v1")
        binding = session.get(PromptBindingRow, "prompt_binding_prompt_huazi_subagent")

    assert template is not None and template.status == "active"
    assert template.output_schema_ref == {"schema_id": "prompt.huazi.output"}
    assert version is not None and version.status == "published"
    assert version.content == seed_item["content"]
    assert binding is not None
    assert binding.node_id == "HuaziPlanningSubagent"
    assert binding.prompt_version_id == "prompt_huazi_subagent_v1"


def test_upgrade_is_idempotent(db_session_factory):
    _run_upgrade(db_session_factory)
    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        template_count = conn.execute(
            text("select count(*) from prompt_templates where id = 'prompt_huazi_subagent'")
        ).scalar_one()
        binding_count = conn.execute(
            text(
                "select count(*) from prompt_bindings "
                "where id = 'prompt_binding_prompt_huazi_subagent'"
            )
        ).scalar_one()

    assert template_count == 1
    assert binding_count == 1
