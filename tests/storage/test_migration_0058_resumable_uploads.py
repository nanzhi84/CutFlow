from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from packages.core.contracts import utcnow
from packages.core.storage.database import ArtifactRow, UploadSessionRow
from packages.core.storage.repository import new_id

MIGRATION_PATH = Path("packages/core/storage/alembic/versions/0058_resumable_uploads.py")
REVISION = "0058_resumable_uploads"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0058", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(engine, fn) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            fn()


def _upload(upload_id: str, *, status: str, object_uri: str | None) -> UploadSessionRow:
    now = utcnow()
    return UploadSessionRow(
        id=upload_id,
        client_upload_id=f"client_{upload_id}",
        owner_user_id="usr_admin",
        kind="font",
        filename=f"{upload_id}.ttf",
        content_type="font/ttf",
        size_bytes=128,
        sha256="a" * 64,
        client_expected_sha256="a" * 64,
        canonical_sha256="a" * 64 if status == "ready" else None,
        status=status,
        upload_strategy="single",
        object_uri=object_uri,
        staging_uri=object_uri,
        final_uri=object_uri if status == "ready" else None,
        completion_metadata={},
        expires_at=now + timedelta(days=1),
    )


def test_migration_revision_is_in_current_single_head_chain() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    migration = script.get_revision(REVISION)

    assert script.get_heads() == ["0063_workflow_cancel_request"]
    assert migration is not None
    assert migration.down_revision == "0057_drop_provider_retry_policy"
    assert len(REVISION) <= 32


def test_schema_has_upload_idempotency_and_artifact_source_constraints(
    db_session_factory,
) -> None:
    engine = db_session_factory.kw["bind"]
    inspector = inspect(engine)
    upload_columns = {column["name"] for column in inspector.get_columns("upload_sessions")}
    artifact_columns = {column["name"] for column in inspector.get_columns("artifacts")}
    upload_unique = {
        constraint["name"] for constraint in inspector.get_unique_constraints("upload_sessions")
    }
    artifact_unique = {
        constraint["name"] for constraint in inspector.get_unique_constraints("artifacts")
    }

    assert {
        "client_upload_id",
        "multipart_upload_id",
        "normalized",
        "staging_uri",
        "final_uri",
        "client_expected_sha256",
        "canonical_sha256",
        "completion_metadata",
        "verified_media_info",
        "retry_count",
        "next_retry_at",
        "lease_owner",
        "lease_expires_at",
    } <= upload_columns
    assert "source_upload_session_id" in artifact_columns
    assert "uq_upload_sessions_client_upload_id" in upload_unique
    assert "uq_artifacts_source_upload_session_id" in artifact_unique


def test_upgrade_backfills_ready_verified_and_auditable_failed_legacy_rows(
    db_session_factory,
) -> None:
    engine = db_session_factory.kw["bind"]
    ready_id = new_id("upl")
    recoverable_id = new_id("upl")
    broken_id = new_id("upl")
    ready_uri = f"local://cutagent-local/font/{ready_id}/ready.ttf"
    recoverable_uri = f"local://cutagent-local/font/{recoverable_id}/recoverable.ttf"

    with db_session_factory() as session:
        session.add_all(
            [
                _upload(ready_id, status="ready", object_uri=ready_uri),
                _upload(recoverable_id, status="verified", object_uri=recoverable_uri),
                _upload(broken_id, status="completed", object_uri=None),
            ]
        )
        session.flush()
        session.add(
            ArtifactRow(
                id=new_id("art"),
                kind="uploaded.file",
                uri=ready_uri,
                size_bytes=128,
                sha256="a" * 64,
                payload_schema="UploadedFileArtifact.v1",
                payload={"id": ready_id},
                source_upload_session_id=ready_id,
            )
        )
        session.commit()

    migration = _load_migration()
    _run(engine, migration.downgrade)
    _run(engine, migration.upgrade)

    with engine.connect() as connection:
        ready = connection.execute(
            text("select status, client_upload_id from upload_sessions where id = :id"),
            {"id": ready_id},
        ).one()
        recoverable = connection.execute(
            text("select status, last_error from upload_sessions where id = :id"),
            {"id": recoverable_id},
        ).one()
        broken = connection.execute(
            text("select status, last_error from upload_sessions where id = :id"),
            {"id": broken_id},
        ).one()
        source_id = connection.execute(
            text("select source_upload_session_id from artifacts where payload ->> 'id' = :id"),
            {"id": ready_id},
        ).scalar_one()

    assert ready.status == "ready"
    assert ready.client_upload_id == ready_id
    assert source_id == ready_id
    assert recoverable.status == "verified"
    assert "queued for registration" in recoverable.last_error
    assert broken.status == "failed"
    assert "could not be reconstructed" in broken.last_error
