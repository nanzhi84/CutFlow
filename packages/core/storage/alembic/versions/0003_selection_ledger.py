from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_selection_ledger"
down_revision = "0002_provider_balance_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001_initial_schema bootstraps the whole current schema via
    # Base.metadata.create_all(), which already includes this table and its indexes
    # on fresh databases (selection_ledger is declared on the ORM model). Guard each
    # object so this migration is a no-op there, while still applying on databases
    # provisioned before the model gained selection_ledger.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("selection_ledger"):
        op.create_table(
            "selection_ledger",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("case_id", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("medium", sa.String(), nullable=False),
            sa.Column("asset_id", sa.String(), nullable=False),
            sa.Column("slot_phase", sa.String(), nullable=False),
            sa.Column("diversity_key", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "case_id",
                "run_id",
                "medium",
                "asset_id",
                "slot_phase",
                name="uq_selection_ledger_case_id",
            ),
        )
    existing_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("selection_ledger")}
    if "idx_selection_ledger_case_medium" not in existing_indexes:
        op.create_index("idx_selection_ledger_case_medium", "selection_ledger", ["case_id", "medium"])
    if "idx_selection_ledger_asset" not in existing_indexes:
        op.create_index("idx_selection_ledger_asset", "selection_ledger", ["medium", "asset_id"])


def downgrade() -> None:
    op.drop_index("idx_selection_ledger_asset", table_name="selection_ledger")
    op.drop_index("idx_selection_ledger_case_medium", table_name="selection_ledger")
    op.drop_table("selection_ledger")
