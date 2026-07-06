from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0038_clip_embedding_jobs"
down_revision = "0037_clip_emb_vector_hnsw"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("clip_embedding_jobs"):
        return
    op.create_table(
        "clip_embedding_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_clip_embedding_jobs_case_id", "clip_embedding_jobs", ["case_id"])


def downgrade() -> None:
    if not _has_table("clip_embedding_jobs"):
        return
    op.drop_index("idx_clip_embedding_jobs_case_id", table_name="clip_embedding_jobs")
    op.drop_table("clip_embedding_jobs")
