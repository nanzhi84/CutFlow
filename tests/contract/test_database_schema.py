import re
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from packages.core.storage.database import Base, table_names


REQUIRED_TABLES = {
    "users",
    "sessions",
    "registration_codes",
    "upload_sessions",
    "secrets",
    "cases",
    "jobs",
    "workflow_runs",
    "node_runs",
    "artifacts",
    "media_assets",
    "annotations",
    "clip_embedding_index",
    "voice_profiles",
    "provider_profiles",
    "provider_capabilities",
    "provider_balance_snapshots",
    "selection_ledger",
    "selection_reservations",
    "provider_invocations",
    "usage_meter_records",
    "provider_price_catalogs",
    "provider_price_items",
    "prompt_templates",
    "prompt_versions",
    "prompt_bindings",
    "prompt_invocations",
    "prompt_experiments",
    "script_versions",
    "video_versions",
    "script_drafts",
    "case_memories",
    "publish_records",
    "performance_observations",
    "creative_feature_vectors",
    "performance_scores",
    "case_rubrics",
    "score_predictions",
    "reward_signals",
    "rubric_bump_proposals",
    "finished_videos",
    "publish_packages",
    "publish_batches",
    "publish_batch_items",
    "publish_attempts",
    "yield_funnel_events",
    "cost_rollups",
    "budgets",
    "provider_billing_reconciliations",
    "ops_alert_rules",
    "ops_alert_events",
    "production_quality_checks",
    "failure_taxonomy",
    "approval_requests",
    "audit_events",
    "import_batch_reports",
    "outbox_events",
    "idempotency_records",
}


def test_sqlalchemy_metadata_covers_spec_table_families():
    missing = REQUIRED_TABLES - table_names()
    assert not missing


def test_contract_columns_for_core_boundaries_exist():
    tables = Base.metadata.tables
    assert {"email", "password_hash", "role", "status"} <= set(tables["users"].columns.keys())
    assert "code_hash" in tables["registration_codes"].columns.keys()
    assert "secret_ref" in tables["secrets"].columns.keys()
    assert "encrypted_value" in tables["secrets"].columns.keys()
    assert {"payload_schema", "schema_version", "payload"} <= set(
        tables["artifacts"].columns.keys()
    )
    assert {"input_manifest_hash", "output_artifact_ids", "provider_invocation_ids"} <= set(
        tables["node_runs"].columns.keys()
    )
    assert {
        "provider_id",
        "account_group",
        "balance_amount",
        "currency",
        "quota_remaining",
        "unit",
        "status",
        "detail",
        "checked_at",
    } <= set(tables["provider_balance_snapshots"].columns.keys())
    assert {
        "case_id",
        "run_id",
        "medium",
        "asset_id",
        "clip_id",
        "slot_phase",
        "diversity_key",
        "created_at",
    } <= set(tables["selection_ledger"].columns.keys())
    assert {
        "case_id",
        "run_id",
        "medium",
        "asset_id",
        "status",
        "expires_at",
        "committed_at",
        "released_at",
    } <= set(tables["selection_reservations"].columns.keys())
    assert {
        "provider_id",
        "model_id",
        "capability_id",
        "price_item_id",
        "billing_status",
        "estimated_cost",
        "started_at",
        "finished_at",
    } <= set(tables["provider_invocations"].columns.keys())
    assert {
        "cached_input_tokens",
        "audio_seconds",
        "video_seconds",
        "image_count",
        "provider_credits",
        "raw_usage",
    } <= set(tables["usage_meter_records"].columns.keys())
    assert "media_seconds" not in tables["usage_meter_records"].columns.keys()
    assert {"created_by", "request_schema", "active_run_id", "latest_finished_video_id"} <= set(
        tables["jobs"].columns.keys()
    )
    assert {"requested_by", "retry_of_run_id", "experiment_assignment_id"} <= set(
        tables["workflow_runs"].columns.keys()
    )
    assert {"attempt", "skipped_reason", "degradation_reason"} <= set(
        tables["node_runs"].columns.keys()
    )
    assert {"variables_schema_ref", "output_schema_ref"} <= set(
        tables["prompt_templates"].columns.keys()
    )
    assert {
        "topic",
        "aggregate_type",
        "aggregate_id",
        "dedupe_key",
        "payload_schema",
        "payload",
        "status",
        "attempts",
        "available_at",
        "created_at",
        "published_at",
        "last_error",
    } <= set(tables["outbox_events"].columns.keys())
    assert {
        "job_id",
        "run_id",
        "finished_video_id",
        "publish_package_id",
        "publish_attempt_id",
        "event_type",
        "event_time",
        "dedupe_key",
    } <= set(tables["yield_funnel_events"].columns.keys())
    assert {
        "key",
        "method",
        "path",
        "request_hash",
        "response_status",
        "response_body",
        "created_at",
        "expires_at",
    } <= set(tables["idempotency_records"].columns.keys())
    assert "enforce" in tables["budgets"].columns.keys()
    assert {
        "provider_id",
        "window_start",
        "window_end",
        "status",
        "dry_run",
        "estimated_cost",
        "recorded_usage_cost",
        "variance",
        "line_items",
        "request_id",
    } <= set(tables["provider_billing_reconciliations"].columns.keys())
    assert "embedding" in tables["case_memories"].columns.keys()


def test_metadata_compiles_for_postgresql():
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        sql = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in sql
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            sql = str(CreateIndex(index).compile(dialect=dialect))
            assert "CREATE" in sql and "INDEX" in sql


def test_artifacts_run_id_indexes_exist():
    artifacts = Base.metadata.tables["artifacts"]
    index_columns = {idx.name: [col.name for col in idx.columns] for idx in artifacts.indexes}
    assert "idx_artifacts_run" in index_columns
    assert index_columns["idx_artifacts_run"] == ["run_id"]
    assert "idx_artifacts_run_kind" in index_columns
    assert index_columns["idx_artifacts_run_kind"] == ["run_id", "kind"]


def test_selection_reservation_active_slot_unique_index_exists():
    reservations = Base.metadata.tables["selection_reservations"]
    indexes = {idx.name: idx for idx in reservations.indexes}

    active_unique = indexes["uq_selection_reservations_active_slot"]
    assert active_unique.unique is True
    assert [col.name for col in active_unique.columns] == ["case_id", "medium", "asset_id"]
    where = active_unique.dialect_options["postgresql"]["where"]
    assert where is not None
    where_sql = str(
        where.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "reserved" in where_sql
    assert "committed" not in where_sql


def test_clip_embedding_index_keys_and_lookup_indexes_exist():
    clip_index = Base.metadata.tables["clip_embedding_index"]
    indexes = {idx.name: [col.name for col in idx.columns] for idx in clip_index.indexes}
    assert set(clip_index.primary_key.columns.keys()) == {"clip_embedding_key"}
    assert indexes["idx_clip_embedding_asset"] == ["asset_id", "index_namespace"]
    assert indexes["idx_clip_embedding_model_version"] == [
        "index_namespace",
        "embedding_model",
        "embedding_dimension",
        "index_version",
    ]


def test_case_rubric_indexes_and_uniques_exist():
    case_rubrics = Base.metadata.tables["case_rubrics"]
    score_predictions = Base.metadata.tables["score_predictions"]
    reward_signals = Base.metadata.tables["reward_signals"]
    rubric_bumps = Base.metadata.tables["rubric_bump_proposals"]

    indexes = {
        index.name: index
        for table in (case_rubrics, score_predictions, reward_signals, rubric_bumps)
        for index in table.indexes
    }
    assert [col.name for col in indexes["idx_case_rubrics_case_status"].columns] == [
        "case_id",
        "status",
    ]
    assert [col.name for col in indexes["idx_score_predictions_case"].columns] == ["case_id"]
    assert [col.name for col in indexes["idx_score_predictions_draft"].columns] == [
        "script_draft_id"
    ]
    assert [col.name for col in indexes["idx_reward_signals_case"].columns] == ["case_id"]
    assert [col.name for col in indexes["idx_rubric_bump_case_status"].columns] == [
        "case_id",
        "status",
    ]

    active_unique = indexes["uq_case_rubrics_active_case"]
    assert active_unique.unique is True
    assert [col.name for col in active_unique.columns] == ["case_id"]
    assert active_unique.dialect_options["postgresql"]["where"] is not None

    reward_unique = indexes["uq_reward_signals_case_source_evidence"]
    assert reward_unique.unique is True
    assert [col.name for col in reward_unique.columns] == [
        "case_id",
        "source_kind",
        "evidence_ref",
    ]
    assert reward_unique.dialect_options["postgresql"]["where"] is not None


def test_case_rubric_server_defaults_match_migration_path():
    tables = Base.metadata.tables
    expected_defaults = {
        "case_rubrics": {
            "schema_version": "v1",
            "version": "1",
            "status": "active",
            "dimensions": "[]",
            "fitted_from_sample_size": "0",
            "cold_start": "true",
        },
        "score_predictions": {
            "schema_version": "v1",
            "rubric_version": "1",
            "composite": "0",
            "band": "ok",
            "dimension_scores": "{}",
            "reason": "",
            "locked_at": "now()",
        },
        "reward_signals": {
            "schema_version": "v1",
            "value": "0",
            "confidence": "0.5",
            "occurred_at": "now()",
        },
        "rubric_bump_proposals": {
            "schema_version": "v1",
            "status": "proposed",
            "from_version": "1",
            "old_consistency": "0",
            "new_consistency": "0",
            "sample_size": "0",
            "rationale": "",
        },
    }
    for table_name, columns in expected_defaults.items():
        for column_name, expected in columns.items():
            default = tables[table_name].columns[column_name].server_default
            assert default is not None, f"{table_name}.{column_name}"
            assert expected in str(default.arg)


def test_alembic_artifacts_run_index_revision_exists():
    migration = Path("packages/core/storage/alembic/versions/0004_artifacts_run_index.py")
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision = "0003_selection_ledger"' in text
    assert 'op.create_index("idx_artifacts_run", "artifacts", ["run_id"])' in text
    assert 'op.create_index("idx_artifacts_run_kind", "artifacts", ["run_id", "kind"])' in text


def test_alembic_initial_revision_exists():
    migration = Path("packages/core/storage/alembic/versions/0001_initial_schema.py")
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in text
    assert "Base.metadata.create_all" in text


def test_alembic_selection_ledger_clip_revision_exists():
    migration = Path("packages/core/storage/alembic/versions/0012_selection_ledger_clip_id.py")
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'revision = "0012_selection_ledger_clip_id"' in text
    assert 'down_revision = "0011_finished_video_lipsync"' in text
    assert '"clip_id"' in text


def test_alembic_case_rubric_indexes_revision_exists():
    migration = Path("packages/core/storage/alembic/versions/0012_case_rubric.py")
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    for name in (
        "idx_case_rubrics_case_status",
        "idx_score_predictions_case",
        "idx_score_predictions_draft",
        "idx_reward_signals_case",
        "idx_rubric_bump_case_status",
        "uq_case_rubrics_active_case",
        "uq_reward_signals_case_source_evidence",
    ):
        assert name in text


def test_alembic_selection_reservation_active_slot_revision_exists():
    migration = Path("packages/core/storage/alembic/versions/0020_selection_reservation_active_slot.py")
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'revision = "0020_resv_active_slot"' in text
    assert 'down_revision = "0019_user_generation_defaults"' in text
    assert "uq_selection_reservations_active_slot" in text


def test_alembic_revision_ids_fit_version_table_limit():
    for migration in Path("packages/core/storage/alembic/versions").glob("*.py"):
        text = migration.read_text(encoding="utf-8")
        match = re.search(r'^revision = "([^"]+)"', text, flags=re.MULTILINE)
        if match is None:
            continue
        assert len(match.group(1)) <= 32, migration
