from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0036_clip_embedding_index"
down_revision = "0035_drop_portrait_reuse_warn"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("clip_embedding_index"):
        return
    op.create_table(
        "clip_embedding_index",
        sa.Column("clip_embedding_key", sa.String(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.String(),
            sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_revision", sa.String(), nullable=False),
        sa.Column("clip_id", sa.String(), nullable=False),
        sa.Column("source_start", sa.Float(), nullable=False),
        sa.Column("source_end", sa.Float(), nullable=False),
        sa.Column("source_frames_available", sa.Integer(), nullable=False),
        sa.Column("index_namespace", sa.String(), nullable=False),
        sa.Column("embedding_scope", sa.String(), nullable=False, server_default="clip"),
        sa.Column("embedding_input_type", sa.String(), nullable=False, server_default="video_clip"),
        sa.Column("embedding_input_ref", sa.String(), nullable=False),
        sa.Column("sample_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding_id", sa.String(), nullable=False),
        sa.Column("embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider_profile_id", sa.String(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("normalization", sa.String(), nullable=False),
        sa.Column("instruct", sa.String(), nullable=False),
        sa.Column("index_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_clip_embedding_asset",
        "clip_embedding_index",
        ["asset_id", "index_namespace"],
    )
    op.create_index(
        "idx_clip_embedding_model_version",
        "clip_embedding_index",
        ["index_namespace", "embedding_model", "embedding_dimension", "index_version"],
    )


def downgrade() -> None:
    if not _has_table("clip_embedding_index"):
        return
    op.drop_index("idx_clip_embedding_model_version", table_name="clip_embedding_index")
    op.drop_index("idx_clip_embedding_asset", table_name="clip_embedding_index")
    op.drop_table("clip_embedding_index")
