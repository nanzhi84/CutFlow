from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text

MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0054_tts_single_mp3.py")
REVISION = "0054_tts_single_mp3"
PROFILE_ID = "volcengine.tts.prod"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0053", MIGRATION_PATH)
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


def test_migration_revision_preserves_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    migration = script.get_revision(REVISION)

    assert heads == ["0058_resumable_uploads"]
    assert migration is not None
    assert migration.down_revision == "0053_postprocess_local_solver"
    assert len(REVISION) <= 32


def test_upgrade_switches_only_delivery_version_and_preserves_options(
    db_session_factory,
) -> None:
    with db_session_factory() as session:
        session.execute(
            text(
                """
                update provider_profiles
                set default_options = jsonb_build_object(
                    'appid', 'keep-appid',
                    'format', 'mp3',
                    'cluster', 'volcano_icl',
                    'api_version', 'v3'
                )
                where id = :profile_id
                """
            ),
            {"profile_id": PROFILE_ID},
        )
        session.commit()

    _run_upgrade(db_session_factory)
    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        options = conn.execute(
            text("select default_options from provider_profiles where id = :profile_id"),
            {"profile_id": PROFILE_ID},
        ).scalar_one()

    assert options == {
        "appid": "keep-appid",
        "format": "mp3",
        "cluster": "volcano_icl",
        "api_version": "v1",
    }
