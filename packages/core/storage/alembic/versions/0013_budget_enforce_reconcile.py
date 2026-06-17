from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0013_budget_enforce_reconcile"
down_revision = "0012_selection_ledger_clip_id"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _add_missing_columns(bind, table: str, columns) -> None:
    existing = {col["name"] for col in sa.inspect(bind).get_columns(table)}
    for name, type_, nullable, server_default in columns:
        if name in existing:
            continue
        op.add_column(
            table,
            sa.Column(name, type_, nullable=nullable, server_default=server_default),
        )


def upgrade() -> None:
    bind = op.get_bind()
    _add_missing_columns(
        bind,
        "budgets",
        (("enforce", sa.Boolean(), False, sa.false()),),
    )
    if not _has_table(bind, "provider_billing_reconciliations"):
        op.create_table(
            "provider_billing_reconciliations",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("provider_id", sa.String(), nullable=True),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("estimated_cost", JSONB(), nullable=False),
            sa.Column("recorded_usage_cost", JSONB(), nullable=False),
            sa.Column("variance", JSONB(), nullable=False),
            sa.Column("line_items", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("request_id", sa.String(), nullable=False),
            sa.Column("schema_version", sa.String(length=16), nullable=False, server_default=sa.text("'v1'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "provider_billing_reconciliations"):
        op.drop_table("provider_billing_reconciliations")
    existing = {col["name"] for col in sa.inspect(bind).get_columns("budgets")}
    if "enforce" in existing:
        op.drop_column("budgets", "enforce")
