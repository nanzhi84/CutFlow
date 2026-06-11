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
    "voice_profiles",
    "provider_profiles",
    "provider_capabilities",
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
    "case_agent_source_bindings",
    "case_agent_runs",
    "creative_briefs",
    "script_drafts",
    "case_memories",
    "memory_proposals",
    "reflection_runs",
    "publish_records",
    "performance_observations",
    "finished_videos",
    "publish_packages",
    "publish_batches",
    "publish_batch_items",
    "publish_attempts",
    "yield_funnel_events",
    "cost_rollups",
    "budgets",
    "ops_alert_events",
    "production_quality_checks",
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
    assert "encrypted_value" not in tables["secrets"].columns.keys()
    assert {"payload_schema", "schema_version", "payload"} <= set(tables["artifacts"].columns.keys())
    assert {"input_manifest_hash", "output_artifact_ids", "provider_invocation_ids"} <= set(
        tables["node_runs"].columns.keys()
    )
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
    assert {"attempt", "skipped_reason", "degradation_reason"} <= set(tables["node_runs"].columns.keys())
    assert {"variables_schema_ref", "output_schema_ref"} <= set(tables["prompt_templates"].columns.keys())
    assert {"topic", "aggregate_type", "aggregate_id", "dedupe_key", "payload_schema"} <= set(
        tables["outbox_events"].columns.keys()
    )
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
    assert "embedding" in tables["case_memories"].columns.keys()


def test_metadata_compiles_for_postgresql():
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        sql = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in sql
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            sql = str(CreateIndex(index).compile(dialect=dialect))
            assert "CREATE INDEX" in sql


def test_alembic_initial_revision_exists():
    migration = Path("packages/core/storage/alembic/versions/0001_initial_schema.py")
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in text
    assert "Base.metadata.create_all" in text
