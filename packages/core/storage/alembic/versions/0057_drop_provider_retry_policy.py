from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0057_drop_provider_retry_policy"
down_revision = "0056_media_prompt_domains"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Drop the profile-level retry setting that no provider runtime consumed."""

    if _has_column("provider_profiles", "retry_policy"):
        op.drop_column("provider_profiles", "retry_policy")


def downgrade() -> None:
    if not _has_column("provider_profiles", "retry_policy"):
        op.add_column(
            "provider_profiles",
            sa.Column(
                "retry_policy",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
        )
