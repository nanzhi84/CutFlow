from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_provider_balance_snapshots"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001_initial_schema bootstraps the whole current schema via
    # Base.metadata.create_all(), which already includes this table and its index
    # on fresh databases (provider_balance_snapshots is declared on the ORM model).
    # Guard each object so this migration is a no-op there, while still applying on
    # databases provisioned before the model gained provider_balance_snapshots.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("provider_balance_snapshots"):
        op.create_table(
            "provider_balance_snapshots",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("provider_id", sa.String(), nullable=False),
            sa.Column("account_group", sa.String(), nullable=True),
            sa.Column("balance_amount", sa.Numeric(20, 6), nullable=True),
            sa.Column("currency", sa.String(3), nullable=True),
            sa.Column("quota_remaining", sa.Float(), nullable=True),
            sa.Column("unit", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("schema_version", sa.String(16), nullable=False, server_default="v1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    existing_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("provider_balance_snapshots")}
    if "idx_provider_balance_snapshots_provider" not in existing_indexes:
        op.create_index(
            "idx_provider_balance_snapshots_provider",
            "provider_balance_snapshots",
            ["provider_id", "account_group"],
        )


def downgrade() -> None:
    op.drop_index("idx_provider_balance_snapshots_provider", table_name="provider_balance_snapshots")
    op.drop_table("provider_balance_snapshots")
