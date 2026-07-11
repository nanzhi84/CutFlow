from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0047_media_selection_diversity.py"
)
SEED_PATH = Path("packages/core/storage/prompt_group_defaults.json")
VERSION_ID = "prompt_media_selection_agent_v1"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0047", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_content() -> str:
    payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return next(
        str(item["content"])
        for item in payload["items"]
        if item["version_id"] == VERSION_ID
    )


def _run_upgrade(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()


def test_migration_revision_chains_to_single_head() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0047_media_selection_diversity"' in source
    assert 'down_revision = "0046_huazi_subagent_prompt"' in source
    assert len("0047_media_selection_diversity") <= 32


def test_upgrade_syncs_media_selection_diversity_contract(db_session_factory) -> None:
    legacy_content = (
        "B-roll 候选（candidate_id | asset_id | scene_name | allowed_slot_ids）：\n"
        "{broll_candidates}"
    )
    with db_session_factory() as session:
        session.execute(
            text("update prompt_versions set content = :content where id = :id"),
            {"content": legacy_content, "id": VERSION_ID},
        )
        session.commit()

    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        content = conn.execute(
            text("select content from prompt_versions where id = :id"),
            {"id": VERSION_ID},
        ).scalar_one()

    assert content == _seed_content()
    assert "candidate_id | asset_id | diversity_key | scene_name" in content
    assert "{broll_uniqueness_rule}" in content


def test_upgrade_is_idempotent(db_session_factory) -> None:
    _run_upgrade(db_session_factory)
    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        count = conn.execute(
            text("select count(*) from prompt_versions where id = :id"),
            {"id": VERSION_ID},
        ).scalar_one()

    assert count == 1
