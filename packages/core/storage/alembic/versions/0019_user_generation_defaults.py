from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019_user_generation_defaults"
down_revision = "0018_owner_user_id_isolation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_generation_defaults" in inspector.get_table_names():
        return
    op.create_table(
        "user_generation_defaults",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "preset_name", sa.String(), nullable=False, server_default="default"
        ),
        sa.Column("settings", JSONB(), nullable=False, server_default="{}"),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", name="uq_user_generation_defaults_user_id"),
    )
    op.create_index(
        "ix_user_generation_defaults_user_id",
        "user_generation_defaults",
        ["user_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_generation_defaults" not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes("user_generation_defaults")}
    if "ix_user_generation_defaults_user_id" in indexes:
        op.drop_index(
            "ix_user_generation_defaults_user_id",
            table_name="user_generation_defaults",
        )
    op.drop_table("user_generation_defaults")
