from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from packages.core.storage.database import (
    ArtifactRow,
    CaseRow,
    JobRow,
    NodeRunRow,
    PromptBindingRow,
    PromptTemplateRow,
    PromptVersionRow,
    UserGenerationDefaultsRow,
    UserRow,
    WorkflowRunRow,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import new_id
from packages.production.sqlalchemy_repository import SqlAlchemyProductionRepository


_MIGRATION_DIR = Path("packages/core/storage/alembic/versions")


def _load(filename: str):
    path = _MIGRATION_DIR / filename
    spec = importlib.util.spec_from_file_location(f"_migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _upgrade(engine, filename: str) -> None:
    module = _load(filename)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            module.upgrade()


def test_caption_cleanslate_migrations_form_the_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["0063_workflow_cancel_request"]
    assert script.get_revision("0059_bgm_agent_prompt").down_revision == (
        "0058_resumable_uploads"
    )
    assert script.get_revision("0060_creative_intent_runs").down_revision == (
        "0059_bgm_agent_prompt"
    )
    assert script.get_revision("0061_caption_cleanslate_cleanup").down_revision == (
        "0060_creative_intent_runs"
    )
    assert script.get_revision("0062_drop_v1_prompts").down_revision == (
        "0061_caption_cleanslate_cleanup"
    )
    assert all(
        len(revision) <= 32
        for revision in (
            "0059_bgm_agent_prompt",
            "0060_creative_intent_runs",
            "0061_caption_cleanslate_cleanup",
            "0062_drop_v1_prompts",
        )
    )


def test_0058_and_0059_publish_current_prompt_contracts_idempotently(
    db_session_factory,
) -> None:
    engine = db_session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text("delete from prompt_bindings where prompt_template_id = 'prompt_bgm_agent'")
        )
        connection.execute(
            text("delete from prompt_versions where prompt_template_id = 'prompt_bgm_agent'")
        )
        connection.execute(text("delete from prompt_templates where id = 'prompt_bgm_agent'"))
        connection.execute(
            text(
                "update prompt_versions set content = 'legacy minimum emphasis prompt' "
                "where id = 'prompt_creative_intent_v1'"
            )
        )

    for _ in range(2):
        _upgrade(engine, "0059_bgm_agent_prompt.py")
        _upgrade(engine, "0060_creative_intent_runs.py")

    with engine.connect() as connection:
        bgm_content = connection.execute(
            text("select content from prompt_versions where id = 'prompt_bgm_agent_v1'")
        ).scalar_one()
        binding = connection.execute(
            text(
                "select prompt_template_id, prompt_version_id, node_id "
                "from prompt_bindings where id = 'prompt_binding_prompt_bgm_agent'"
            )
        ).one()
        intent_content = connection.execute(
            text("select content from prompt_versions where id = 'prompt_creative_intent_v1'")
        ).scalar_one()

    assert binding == ("prompt_bgm_agent", "prompt_bgm_agent_v1", "BgmAgentPlanning")
    assert "只能输出 bgm_id 和 analysis" in bgm_content
    assert "caption" not in bgm_content.lower()
    assert "display_mode" in intent_content
    assert "允许空" in intent_content or "可为空" in intent_content


def test_0060_cleans_caption_rows_reports_and_emphasis_only_settings_idempotently(
    db_session_factory,
) -> None:
    engine = db_session_factory.kw["bind"]
    job_id = new_id("job")
    run_id = new_id("run")
    node_id = new_id("node")
    old_artifact_id = new_id("art")
    style_artifact_id = new_id("art")
    report_artifact_id = new_id("art")
    user_id = new_id("usr")
    defaults_id = new_id("ugd")
    subtitle = {
        "enabled": False,
        "normal_enabled": True,
        "emphasis_enabled": True,
        "caption_style_pair_id": "retired",
        "emphasis_position_id": "retired",
        "emphasis_animation_id": "retired",
    }
    valid_degradation = {
        "code": "sfx.asset_missing",
        "message": "keep",
        "node_id": "SubtitleAndBgmMix",
        "affects_true_yield": False,
    }
    retired_degradation = {
        "code": "caption.visual_analysis_failed",
        "message": "remove",
        "node_id": "CaptionWindowPlanning",
        "affects_true_yield": False,
    }

    with db_session_factory() as session:
        session.add(
            JobRow(
                id=job_id,
                type="digital_human_video",
                status="failed",
                request_schema="DigitalHumanVideoRequest.v1",
                request={"subtitle": subtitle},
            )
        )
        session.flush()
        session.add(
            WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                workflow_template_id="digital_human_editing_agent_v2",
                workflow_version="v2",
                status="failed",
            )
        )
        session.flush()
        session.add_all(
            [
                ArtifactRow(
                    id=old_artifact_id,
                    run_id=run_id,
                    kind="plan.caption_windows",
                    payload_schema="CaptionWindowsPlan.v2",
                    payload={"events": []},
                ),
                ArtifactRow(
                    id=style_artifact_id,
                    run_id=run_id,
                    kind="plan.style",
                    payload_schema="StylePlanArtifact.v1",
                    payload={"subtitle": {}, "overlay_events": [{"text": "retired"}]},
                ),
                ArtifactRow(
                    id=report_artifact_id,
                    run_id=run_id,
                    kind="run.report.public",
                    payload_schema="RunReport.v1",
                    payload={
                        "warnings": ["caption.visual_analysis_failed", "sfx.asset_missing"],
                        "degradations": [retired_degradation, valid_degradation],
                    },
                ),
            ]
        )
        session.add(
            NodeRunRow(
                id=node_id,
                run_id=run_id,
                node_id="CaptionWindowPlanning",
                node_version="v2",
                status="degraded",
                input_manifest_hash="sha256:test",
                output_artifact_ids=[old_artifact_id, style_artifact_id],
                warnings=["caption.visual_analysis_failed", "sfx.asset_missing"],
                degradations=[retired_degradation, valid_degradation],
            )
        )
        session.add(
            UserRow(
                id=user_id,
                email=f"{user_id}@example.test",
                display_name="caption cleanup",
                password_hash="unused",
                role="admin",
                status="active",
            )
        )
        session.flush()
        session.add(
            UserGenerationDefaultsRow(
                id=defaults_id,
                user_id=user_id,
                settings={"subtitle": subtitle},
            )
        )
        session.commit()

    _upgrade(engine, "0061_caption_cleanslate_cleanup.py")
    _upgrade(engine, "0061_caption_cleanslate_cleanup.py")

    with db_session_factory() as session:
        node = session.get(NodeRunRow, node_id)
        style = session.get(ArtifactRow, style_artifact_id)
        report = session.get(ArtifactRow, report_artifact_id)
        job = session.get(JobRow, job_id)
        defaults = session.get(UserGenerationDefaultsRow, defaults_id)
        assert session.get(ArtifactRow, old_artifact_id) is None
        assert node.output_artifact_ids == [style_artifact_id]
        assert node.warnings == ["sfx.asset_missing"]
        assert node.degradations == [valid_degradation]
        assert style.payload == {"subtitle": {}}
        assert report.payload["warnings"] == ["sfx.asset_missing"]
        assert report.payload["degradations"] == [valid_degradation]
        for stored_subtitle in (job.request["subtitle"], defaults.settings["subtitle"]):
            assert stored_subtitle["normal_enabled"] is False
            assert stored_subtitle["emphasis_enabled"] is False
            assert "caption_style_pair_id" not in stored_subtitle
            assert "emphasis_position_id" not in stored_subtitle
            assert "emphasis_animation_id" not in stored_subtitle


def test_0061_drops_v1_prompts_and_v1_warning_codes_idempotently(
    db_session_factory,
) -> None:
    engine = db_session_factory.kw["bind"]
    job_id = new_id("job")
    run_id = new_id("run")
    node_run_id = new_id("node")
    old_diagnostics_id = new_id("art")
    report_id = new_id("art")
    old_degradation = {
        "code": "editing_agent.deterministic_fallback",
        "message": "remove",
        "node_id": "EditingAgentPlanning",
        "affects_true_yield": False,
    }
    valid_degradation = {
        "code": "sfx.asset_missing",
        "message": "keep",
        "node_id": "SubtitleAndBgmMix",
        "affects_true_yield": False,
    }

    with db_session_factory() as session:
        session.add(
            PromptTemplateRow(
                id="prompt_editing_agent",
                name="retired",
                purpose="retired",
                variables_schema_ref={"schema_id": "retired.variables"},
                output_schema_ref={"schema_id": "retired.output"},
                status="active",
            )
        )
        session.flush()
        session.add(
            PromptVersionRow(
                id="prompt_editing_agent_v1",
                prompt_template_id="prompt_editing_agent",
                content="retired",
                status="published",
            )
        )
        session.flush()
        session.add(
            PromptBindingRow(
                id="prompt_binding_prompt_editing_agent",
                prompt_template_id="prompt_editing_agent",
                prompt_version_id="prompt_editing_agent_v1",
                node_id="EditingAgentPlanning",
                priority=1,
            )
        )
        session.add(
            JobRow(
                id=job_id,
                type="digital_human_video",
                status="failed",
                request_schema="DigitalHumanVideoRequest.v1",
                request={},
            )
        )
        session.flush()
        session.add(
            WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                workflow_template_id="digital_human_editing_agent_v1",
                workflow_version="v1",
                status="failed",
            )
        )
        session.flush()
        session.add(
            NodeRunRow(
                id=node_run_id,
                run_id=run_id,
                node_id="EditingAgentPlanning",
                node_version="v1",
                status="degraded",
                input_manifest_hash="sha256:test",
                output_artifact_ids=[old_diagnostics_id],
                warnings=["editing_agent.llm_repair", "sfx.asset_missing"],
                degradations=[old_degradation, valid_degradation],
            )
        )
        session.add(
            ArtifactRow(
                id=old_diagnostics_id,
                run_id=run_id,
                kind="plan.editing_diagnostics",
                payload_schema="EditingAgentDiagnostics.v1",
                payload={"retired": True},
            )
        )
        session.add(
            ArtifactRow(
                id=report_id,
                run_id=run_id,
                kind="run.report.debug",
                payload_schema="RunReport.v1",
                payload={
                    "warnings": ["editing_agent.llm_repair", "sfx.asset_missing"],
                    "degradations": [old_degradation, valid_degradation],
                },
            )
        )
        session.commit()

    _upgrade(engine, "0062_drop_v1_prompts.py")
    _upgrade(engine, "0062_drop_v1_prompts.py")

    with db_session_factory() as session:
        assert session.get(PromptTemplateRow, "prompt_editing_agent") is None
        assert session.execute(
            select(PromptVersionRow).where(
                PromptVersionRow.prompt_template_id == "prompt_editing_agent"
            )
        ).scalar_one_or_none() is None
        assert session.execute(
            select(PromptBindingRow).where(
                PromptBindingRow.prompt_template_id == "prompt_editing_agent"
            )
        ).scalar_one_or_none() is None
        node = session.get(NodeRunRow, node_run_id)
        report = session.get(ArtifactRow, report_id)
        assert session.get(ArtifactRow, old_diagnostics_id) is None
        assert node.output_artifact_ids == []
        assert node.warnings == ["sfx.asset_missing"]
        assert node.degradations == [valid_degradation]
        assert report.payload["warnings"] == ["sfx.asset_missing"]
        assert report.payload["degradations"] == [valid_degradation]


def test_historical_prompt_migrations_no_longer_read_mutable_seed_json() -> None:
    frozen_revisions = (
        "0029_sync_editing_agent_prompt.py",
        "0030_sync_editing_agent_prompt.py",
        "0039_sync_editing_agent_prompt_v2.py",
        "0041_bgm_mood_prompt_sync.py",
        "0042_editing_agent_full_coverage_prompt.py",
        "0043_fullcov_single_clip_prompt.py",
        "0044_huazi_prompt_boundary.py",
        "0046_huazi_subagent_prompt.py",
        "0047_media_selection_diversity.py",
        "0048_emphasis_floor_prompts.py",
        "0051_broll_legal_candidates.py",
        "0053_postprocess_local_solver.py",
        "0056_media_prompt_domains.py",
    )

    for filename in frozen_revisions:
        source = (_MIGRATION_DIR / filename).read_text(encoding="utf-8")
        assert "read_text(" not in source
        assert "json.loads(" not in source


def test_real_0057_to_0061_upgrade_keeps_historical_run_apis_readable(
    db_session_factory,
    tmp_path,
) -> None:
    """Exercise the real Alembic chain in an isolated throwaway Postgres database."""

    source_engine = db_session_factory.kw["bind"]
    source_url = make_url(source_engine.url.render_as_string(hide_password=False))
    database_name = f"cutagent_migration_209_{uuid.uuid4().hex[:10]}"
    assert database_name.startswith("cutagent_migration_209_")
    admin_url = source_url.set(database="postgres")
    isolated_url = source_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    isolated_engine = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'create database "{database_name}"'))
        migration_env = dict(os.environ)
        migration_env["CUTAGENT_DATABASE_URL"] = isolated_url.render_as_string(
            hide_password=False
        )
        migration_env["CUTAGENT_STORAGE_BACKEND"] = "sqlalchemy"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                "alembic.ini",
                "upgrade",
                "0057_drop_provider_retry_policy",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=migration_env,
            check=True,
            capture_output=True,
            text=True,
        )
        isolated_engine = create_engine(isolated_url)
        isolated_factory = sessionmaker(bind=isolated_engine, expire_on_commit=False)
        case_id = "case_migration_209"
        job_id = "job_migration_209"
        run_id = "run_migration_209"
        node_id = "node_migration_209"
        old_artifact_id = "art_old_caption_windows"
        style_artifact_id = "art_style_retained"
        report_artifact_id = "art_public_report"
        retired_degradation = {
            "code": "caption.visual_analysis_failed",
            "message": "remove",
            "node_id": "CaptionWindowPlanning",
            "affects_true_yield": False,
        }
        retained_degradation = {
            "code": "sfx.asset_missing",
            "message": "keep",
            "node_id": "SubtitleAndBgmMix",
            "affects_true_yield": False,
        }
        with isolated_factory() as session:
            session.add(
                CaseRow(
                    id=case_id,
                    name="0057 historical run",
                    status="active",
                    key_selling_points=[],
                    strategy_tags=[],
                    brand_keywords=[],
                    competitor_names=[],
                )
            )
            session.add(
                JobRow(
                    id=job_id,
                    type="digital_human_video",
                    status="failed",
                    case_id=case_id,
                    request_schema="DigitalHumanVideoRequest.v1",
                    request={
                        "case_id": case_id,
                        "script": "历史字幕任务",
                        "voice": {"voice_id": "voice_fixture"},
                        "workflow_template_id": "digital_human_editing_agent_v2",
                        "subtitle": {
                            "enabled": False,
                            "normal_enabled": True,
                            "emphasis_enabled": True,
                            "caption_style_pair_id": "retired",
                        },
                    },
                )
            )
            session.flush()
            run_row = WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                case_id=case_id,
                workflow_template_id="digital_human_editing_agent_v2",
                workflow_version="v2",
                status="failed",
            )
            session.add(run_row)
            session.flush()
            session.add_all(
                [
                    ArtifactRow(
                        id=old_artifact_id,
                        run_id=run_id,
                        kind="plan.caption_windows",
                        payload_schema="CaptionWindowsPlan.v2",
                        payload={"events": []},
                    ),
                    ArtifactRow(
                        id=style_artifact_id,
                        run_id=run_id,
                        kind="plan.style",
                        payload_schema="StylePlanArtifact.v1",
                        payload={"subtitle": {}, "overlay_events": [{"text": "old"}]},
                    ),
                    ArtifactRow(
                        id=report_artifact_id,
                        run_id=run_id,
                        kind="run.report.public",
                        payload_schema="RunReport.v1",
                        payload={
                            "run_id": run_id,
                            "status": "failed",
                            "summary": "historical report",
                            "node_statuses": {"CaptionWindowPlanning": "failed"},
                            "warnings": [
                                "caption.visual_analysis_failed",
                                "sfx.asset_missing",
                            ],
                            "degradations": [
                                "caption.visual_analysis_failed",
                                "sfx.asset_missing",
                            ],
                        },
                    ),
                ]
            )
            session.flush()
            run_row.public_report_artifact_id = report_artifact_id
            session.add(
                NodeRunRow(
                    id=node_id,
                    run_id=run_id,
                    node_id="CaptionWindowPlanning",
                    node_version="v2",
                    status="failed",
                    input_manifest_hash="sha256:migration",
                    output_artifact_ids=[old_artifact_id, style_artifact_id],
                    provider_invocation_ids=[],
                    error={
                        "code": "provider.timeout",
                        "message": "historical retryable failure",
                        "retryable": True,
                        "details": {},
                    },
                    warnings=["caption.visual_analysis_failed", "sfx.asset_missing"],
                    degradations=[retired_degradation, retained_degradation],
                )
            )
            session.commit()

        isolated_engine.dispose()
        isolated_engine = None
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[2],
            env=migration_env,
            check=True,
            capture_output=True,
            text=True,
        )
        isolated_engine = create_engine(isolated_url)
        isolated_factory = sessionmaker(bind=isolated_engine, expire_on_commit=False)
        repository = SqlAlchemyProductionRepository(
            isolated_factory,
            object_store=LocalObjectStore(tmp_path / "migration-objects"),
        )

        detail = repository.run_detail(run_id, "req-detail")
        cards = repository.case_run_cards(case_id=case_id, request_id="req-list")
        report = repository.run_report(run_id, "req-report")

        assert detail is not None
        assert {artifact.kind.value for artifact in detail.artifacts} == {
            "plan.style",
            "run.report.public",
        }
        assert detail.artifact_payloads[style_artifact_id] == {"subtitle": {}}
        assert cards is not None and len(cards.items) == 1
        assert cards.items[0].can_resume is False
        assert report is not None
        assert [warning.value for warning in report.public_report.warnings] == [
            "sfx.asset_missing"
        ]
        assert [code.value for code in report.public_report.degradations] == [
            "sfx.asset_missing"
        ]
        with isolated_engine.connect() as connection:
            assert (
                connection.execute(text("select version_num from alembic_version")).scalar_one()
                == "0063_workflow_cancel_request"
            )
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = :database_name and pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'drop database if exists "{database_name}"'))
        admin_engine.dispose()
