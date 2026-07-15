from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text

MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0055_tts_async_icl2.py")
REVISION = "0055_tts_async_icl2"
PROFILE_ID = "volcengine.tts.prod"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0054", MIGRATION_PATH)
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

    assert heads == ["0059_upload_normalized_state"]
    assert migration is not None
    assert migration.down_revision == "0054_tts_single_mp3"
    assert len(REVISION) <= 32


def test_upgrade_switches_to_async_icl2_and_preserves_unrelated_options(
    db_session_factory,
) -> None:
    with db_session_factory() as session:
        session.execute(
            text(
                """
                update provider_profiles
                set model_id = 'seed-audio-1.0',
                    default_options = jsonb_build_object(
                        'appid', 'keep-appid',
                        'cluster', 'volcano_icl',
                        'data_base_url_v3', 'https://voice.example',
                        'format', 'wav',
                        'api_version', 'v1',
                        'resource_id', 'volc.megatts.default',
                        'async_icl2_ready', true,
                        'v3_model', 'seed-tts-2.0-standard',
                        'v3_create_model', 'seed-audio-1.0'
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
        row = conn.execute(
            text(
                """
                select model_id, default_options
                from provider_profiles
                where id = :profile_id
                """
            ),
            {"profile_id": PROFILE_ID},
        ).one()
        capability = conn.execute(
            text(
                """
                select model_id, supports_async_job
                from provider_capabilities
                where provider_id = 'volcengine.tts'
                  and capability_id = 'tts.speech'
                """
            )
        ).one()

    assert row.model_id == "seed-icl-2.0"
    assert row.default_options == {
        "appid": "keep-appid",
        "cluster": "volcano_icl",
        "data_base_url_v3": "https://voice.example",
        "format": "mp3",
        "api_version": "v3",
        "resource_id": "seed-icl-2.0",
        "async_icl2_ready": True,
        "sample_rate": 24000,
        "poll_interval": 1.0,
        "poll_max_attempts": 600,
    }
    assert capability.model_id == "seed-icl-2.0"
    assert capability.supports_async_job is True


def test_upgrade_does_not_activate_v3_before_access_token_is_armed(
    db_session_factory,
) -> None:
    with db_session_factory() as session:
        session.execute(
            text(
                """
                update provider_profiles
                set model_id = 'seed-audio-1.0',
                    default_options = jsonb_build_object(
                        'appid', 'legacy-appid',
                        'api_version', 'v1',
                        'format', 'mp3'
                    )
                where id = :profile_id
                """
            ),
            {"profile_id": PROFILE_ID},
        )
        session.execute(
            text(
                """
                update provider_capabilities
                set model_id = 'seed-audio-1.0',
                    supports_async_job = false
                where provider_id = 'volcengine.tts'
                  and capability_id = 'tts.speech'
                """
            )
        )
        session.commit()

    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        profile = conn.execute(
            text(
                """
                select model_id, default_options
                from provider_profiles
                where id = :profile_id
                """
            ),
            {"profile_id": PROFILE_ID},
        ).one()
        capability = conn.execute(
            text(
                """
                select model_id, supports_async_job
                from provider_capabilities
                where provider_id = 'volcengine.tts'
                  and capability_id = 'tts.speech'
                """
            )
        ).one()

    assert profile.model_id == "seed-audio-1.0"
    assert profile.default_options == {
        "appid": "legacy-appid",
        "api_version": "v1",
        "format": "mp3",
    }
    assert capability.model_id == "seed-audio-1.0"
    assert capability.supports_async_job is True
