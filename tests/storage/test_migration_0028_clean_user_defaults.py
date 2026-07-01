"""Regression for cleaning legacy saved generation defaults.

Migrations 0023/0024 removed contract-deleted nested keys from ``jobs.request``.
Restored legacy databases can also have those same keys in
``user_generation_defaults.settings``; under ``extra="forbid"`` the
``/api/auth/me/generation-defaults`` endpoint rejects the saved preset with a 500.
Migration 0028 strips the retired nested keys from saved defaults too.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import text

from packages.core.contracts import (
    DigitalHumanVideoRequest,
    UserGenerationDefaults,
    VoiceOptions,
)
from packages.core.storage.database import UserGenerationDefaultsRow, UserRow
from packages.core.storage.repository import new_id

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0028_clean_user_defaults_legacy_keys.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0028", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_chains_to_single_head():
    text_src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0028_clean_user_defaults"' in text_src
    assert 'down_revision = "0027_drop_portrait_options"' in text_src
    assert len("0028_clean_user_defaults") <= 32


def _legacy_defaults_with_removed_keys() -> dict:
    request = DigitalHumanVideoRequest(
        case_id="case_legacy", script="hello", voice=VoiceOptions(voice_id="voice_demo")
    ).model_dump(mode="json")
    defaults = UserGenerationDefaults(
        voice=request["voice"],
        broll=request["broll"],
        lipsync=request["lipsync"],
        subtitle=request["subtitle"],
        bgm=request["bgm"],
        cover=request["cover"],
        output=request["output"],
        strictness=request["strictness"],
    ).model_dump(mode="json")
    defaults["lipsync"]["ref_image_artifact_id"] = "art_legacy_ref"
    defaults["lipsync"]["video_extension"] = False
    defaults["lipsync"]["query_face_threshold"] = None
    defaults["output"]["format"] = "mp4"
    defaults["output"]["upload_to_oss"] = True
    defaults["output"]["export_jianying_draft"] = True
    defaults["output"]["export_editor_handoff"] = True
    defaults["output"]["keep_local_originals"] = False
    defaults["strictness"]["broll_insufficient_policy"] = "soft_degrade"
    defaults["strictness"]["bgm_unavailable_policy"] = "soft_degrade"
    defaults["strictness"]["strict_cost_pricing"] = False
    return defaults


def test_upgrade_strips_removed_keys_from_user_generation_defaults(db_session_factory):
    legacy_defaults = _legacy_defaults_with_removed_keys()

    with pytest.raises(ValidationError):
        UserGenerationDefaults.model_validate(legacy_defaults)

    user_id = new_id("usr")
    row_id = new_id("ugd")
    with db_session_factory() as session:
        session.add(
            UserRow(
                id=user_id,
                email="legacy@example.test",
                display_name="legacy",
                password_hash="not-used",
                role="admin",
                status="active",
            )
        )
        session.flush()
        session.add(
            UserGenerationDefaultsRow(
                id=row_id,
                user_id=user_id,
                settings=legacy_defaults,
            )
        )
        session.commit()

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()

    with engine.connect() as conn:
        stored = conn.execute(
            text("select settings from user_generation_defaults where id = :id"),
            {"id": row_id},
        ).scalar_one()

    assert "ref_image_artifact_id" not in stored["lipsync"]
    assert "video_extension" not in stored["lipsync"]
    assert "query_face_threshold" not in stored["lipsync"]
    assert "format" not in stored["output"]
    assert "upload_to_oss" not in stored["output"]
    assert "strict_cost_pricing" not in stored["strictness"]
    assert stored["voice"]["voice_id"] == "voice_demo"
    assert stored["lipsync"]["enabled"] is True
    assert stored["output"]["width"] == 1080

    UserGenerationDefaults.model_validate(stored)
