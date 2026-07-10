"""Regression for cleaning retired subtitle preset fields.

The post-processing UI no longer exposes subtitle combination presets or a fixed
huazi position picker. ``SubtitleOptions`` therefore dropped
``caption_style_pair_id`` and ``emphasis_position_id``. Legacy rows persisted
those keys in ``jobs.request.subtitle`` and saved user defaults, so strict
contract validation rejected them when loading the outputs page or defaults.
Migration 0045 strips the retired nested fields from both places.
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
from packages.core.storage.database import JobRow, UserGenerationDefaultsRow, UserRow
from packages.core.storage.repository import new_id

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0045_drop_subtitle_preset_fields.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0045", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_chains_to_single_head():
    text_src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0045_drop_subtitle_preset"' in text_src
    assert 'down_revision = "0044_huazi_prompt_boundary"' in text_src
    assert len("0045_drop_subtitle_preset") <= 32


def _legacy_request_with_subtitle_presets() -> dict:
    request = DigitalHumanVideoRequest(
        case_id="case_legacy",
        script="hello",
        voice=VoiceOptions(voice_id="voice_demo"),
    ).model_dump(mode="json")
    request["subtitle"]["caption_style_pair_id"] = "douyin_bold_a"
    request["subtitle"]["emphasis_position_id"] = "top_center_banner"
    return request


def _legacy_defaults_with_subtitle_presets() -> dict:
    request = DigitalHumanVideoRequest(
        case_id="case_legacy",
        script="hello",
        voice=VoiceOptions(voice_id="voice_demo"),
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
    defaults["subtitle"]["caption_style_pair_id"] = "douyin_bold_a"
    defaults["subtitle"]["emphasis_position_id"] = "top_center_banner"
    return defaults


def _run_upgrade(db_session_factory) -> None:
    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()


def test_upgrade_strips_subtitle_preset_fields_from_jobs(db_session_factory):
    legacy_request = _legacy_request_with_subtitle_presets()

    with pytest.raises(ValidationError):
        DigitalHumanVideoRequest.model_validate(legacy_request)

    job_id = new_id("job")
    with db_session_factory() as session:
        session.add(
            JobRow(
                id=job_id,
                type="digital_human_video",
                status="succeeded",
                request_schema="DigitalHumanVideoRequest.v1",
                request=legacy_request,
            )
        )
        session.commit()

    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        stored = conn.execute(
            text("select request from jobs where id = :id"), {"id": job_id}
        ).scalar_one()

    subtitle = stored["subtitle"]
    assert "caption_style_pair_id" not in subtitle
    assert "emphasis_position_id" not in subtitle
    assert subtitle["style_preset"] == "douyin"
    assert subtitle["enabled"] is True
    DigitalHumanVideoRequest.model_validate(stored)


def test_upgrade_strips_subtitle_preset_fields_from_user_defaults(db_session_factory):
    legacy_defaults = _legacy_defaults_with_subtitle_presets()

    with pytest.raises(ValidationError):
        UserGenerationDefaults.model_validate(legacy_defaults)

    user_id = new_id("usr")
    row_id = new_id("ugd")
    with db_session_factory() as session:
        session.add(
            UserRow(
                id=user_id,
                email="subtitle-legacy@example.test",
                display_name="subtitle legacy",
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

    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        stored = conn.execute(
            text("select settings from user_generation_defaults where id = :id"),
            {"id": row_id},
        ).scalar_one()

    subtitle = stored["subtitle"]
    assert "caption_style_pair_id" not in subtitle
    assert "emphasis_position_id" not in subtitle
    assert subtitle["font_size"] is None
    assert stored["voice"]["voice_id"] == "voice_demo"
    UserGenerationDefaults.model_validate(stored)


def test_upgrade_is_idempotent_and_skips_non_digital_human_jobs(db_session_factory):
    legacy_request = _legacy_request_with_subtitle_presets()
    dh_id = new_id("job")
    other_id = new_id("job")
    other_request = {"schema_version": "publish_batch_request.v1", "marker": "keep"}

    with db_session_factory() as session:
        session.add(
            JobRow(
                id=dh_id,
                type="digital_human_video",
                status="succeeded",
                request_schema="DigitalHumanVideoRequest.v1",
                request=legacy_request,
            )
        )
        session.add(
            JobRow(
                id=other_id,
                type="publish_batch",
                status="succeeded",
                request_schema="PublishBatchRequest.v1",
                request=other_request,
            )
        )
        session.commit()

    _run_upgrade(db_session_factory)
    _run_upgrade(db_session_factory)

    with db_session_factory.kw["bind"].connect() as conn:
        dh_request = conn.execute(
            text("select request from jobs where id = :id"), {"id": dh_id}
        ).scalar_one()
        kept_request = conn.execute(
            text("select request from jobs where id = :id"), {"id": other_id}
        ).scalar_one()

    assert "caption_style_pair_id" not in dh_request["subtitle"]
    assert "emphasis_position_id" not in dh_request["subtitle"]
    assert kept_request == other_request
