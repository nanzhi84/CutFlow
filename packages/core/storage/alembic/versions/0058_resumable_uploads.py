from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0058_resumable_uploads"
down_revision = "0057_drop_provider_retry_policy"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _column_names(table_name):
        op.add_column(table_name, column)


def _unique_constraint_name(table_name: str, columns: tuple[str, ...]) -> str | None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return None
    expected = set(columns)
    for constraint in inspector.get_unique_constraints(table_name):
        if set(constraint.get("column_names") or ()) == expected:
            return constraint.get("name")
    return None


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return False
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    # 0001 creates Base.metadata on a clean install, so current ORM columns can
    # already exist when Alembic reaches 0058. These guards also preserve the
    # normal path from a real historical 0057 schema.
    upload_columns = (
        sa.Column("client_upload_id", sa.String(), nullable=True),
        sa.Column(
            "owner_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("upload_strategy", sa.String(), server_default="single", nullable=False),
        sa.Column("multipart_upload_id", sa.Text(), nullable=True),
        sa.Column("final_size_bytes", sa.Integer(), nullable=True),
        sa.Column("part_size_bytes", sa.Integer(), nullable=True),
        sa.Column("part_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("normalized", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("staging_uri", sa.Text(), nullable=True),
        sa.Column("final_uri", sa.Text(), nullable=True),
        sa.Column("client_expected_sha256", sa.String(), nullable=True),
        sa.Column("canonical_sha256", sa.String(), nullable=True),
        sa.Column(
            "completion_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("verified_media_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in upload_columns:
        _add_column_if_missing("upload_sessions", column)

    op.execute(
        """
        UPDATE upload_sessions
        SET client_upload_id = id,
            client_expected_sha256 = sha256,
            canonical_sha256 = CASE WHEN status = 'completed' THEN sha256 ELSE NULL END,
            final_size_bytes = CASE WHEN status = 'completed' THEN size_bytes ELSE NULL END,
            staging_uri = object_uri,
            final_uri = CASE WHEN status = 'completed' THEN object_uri ELSE NULL END
        """
    )
    op.alter_column(
        "upload_sessions",
        "client_upload_id",
        nullable=False,
        server_default=sa.text("'legacy_' || gen_random_uuid()::text"),
    )
    if _unique_constraint_name("upload_sessions", ("client_upload_id",)) is None:
        op.create_unique_constraint(
            "uq_upload_sessions_client_upload_id", "upload_sessions", ["client_upload_id"]
        )
    if not _has_index("upload_sessions", "ix_upload_sessions_owner_user_id"):
        op.create_index(
            "ix_upload_sessions_owner_user_id", "upload_sessions", ["owner_user_id"], unique=False
        )
    if not _has_index("upload_sessions", "idx_upload_sessions_reconcile"):
        op.create_index(
            "idx_upload_sessions_reconcile",
            "upload_sessions",
            ["status", "next_retry_at", "lease_expires_at"],
            unique=False,
        )

    _add_column_if_missing(
        "artifacts",
        sa.Column(
            "source_upload_session_id",
            sa.String(),
            sa.ForeignKey("upload_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Legacy UploadedFileArtifact.v1 payloads stored the full UploadSession with
    # its id at the top level. If old retries produced duplicates, bind the newest
    # one and leave the rest as historical unbound artifacts.
    op.execute(
        """
        WITH candidates AS (
            SELECT a.id AS artifact_id,
                   u.id AS upload_id,
                   row_number() OVER (
                       PARTITION BY u.id ORDER BY a.created_at DESC, a.id DESC
                   ) AS rank
            FROM artifacts AS a
            JOIN upload_sessions AS u ON a.payload ->> 'id' = u.id
            WHERE a.kind = 'uploaded.file'
        )
        UPDATE artifacts AS a
        SET source_upload_session_id = candidates.upload_id
        FROM candidates
        WHERE a.id = candidates.artifact_id AND candidates.rank = 1
        """
    )
    if _unique_constraint_name("artifacts", ("source_upload_session_id",)) is None:
        op.create_unique_constraint(
            "uq_artifacts_source_upload_session_id",
            "artifacts",
            ["source_upload_session_id"],
        )

    op.execute(
        """
        UPDATE upload_sessions AS u
        SET status = 'ready'
        WHERE u.status = 'completed'
          AND EXISTS (
              SELECT 1 FROM artifacts AS a
              WHERE a.source_upload_session_id = u.id
          )
        """
    )
    op.execute(
        """
        UPDATE upload_sessions
        SET status = 'verified',
            last_error = 'legacy completed session had no upload artifact; queued for registration'
        WHERE status = 'completed' AND object_uri IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE upload_sessions
        SET status = 'failed',
            last_error = 'legacy completed session could not be reconstructed automatically'
        WHERE status = 'completed'
        """
    )


def downgrade() -> None:
    op.execute(
        "UPDATE upload_sessions SET status = 'completed' WHERE status IN ('ready', 'verified')"
    )
    artifact_constraint = _unique_constraint_name("artifacts", ("source_upload_session_id",))
    if artifact_constraint is not None:
        op.drop_constraint(artifact_constraint, "artifacts", type_="unique")
    if "source_upload_session_id" in _column_names("artifacts"):
        op.drop_column("artifacts", "source_upload_session_id")
    if _has_index("upload_sessions", "idx_upload_sessions_reconcile"):
        op.drop_index("idx_upload_sessions_reconcile", table_name="upload_sessions")
    if _has_index("upload_sessions", "ix_upload_sessions_owner_user_id"):
        op.drop_index("ix_upload_sessions_owner_user_id", table_name="upload_sessions")
    upload_constraint = _unique_constraint_name("upload_sessions", ("client_upload_id",))
    if upload_constraint is not None:
        op.drop_constraint(upload_constraint, "upload_sessions", type_="unique")
    for column in (
        "lease_expires_at",
        "lease_owner",
        "next_retry_at",
        "retry_count",
        "last_error",
        "verified_media_info",
        "completion_metadata",
        "canonical_sha256",
        "client_expected_sha256",
        "final_uri",
        "staging_uri",
        "part_count",
        "part_size_bytes",
        "multipart_upload_id",
        "final_size_bytes",
        "normalized",
        "upload_strategy",
        "owner_user_id",
        "client_upload_id",
    ):
        if column in _column_names("upload_sessions"):
            op.drop_column("upload_sessions", column)
