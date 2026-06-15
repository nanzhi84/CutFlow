from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_selection_reservations"
down_revision = "0006_media_thumbnail_duration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001_initial_schema bootstraps the whole current schema via
    # Base.metadata.create_all(), which already includes this table and its indexes
    # on fresh databases (selection_reservations is declared on the ORM model). Guard
    # each object so this migration is a no-op there, while still applying on databases
    # provisioned before the model gained selection_reservations (Spec §6.6 / §17).
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("selection_reservations"):
        op.create_table(
            "selection_reservations",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("case_id", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("medium", sa.String(), nullable=False),
            sa.Column("asset_id", sa.String(), nullable=False),
            sa.Column("diversity_key", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="reserved"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "run_id",
                "medium",
                "asset_id",
                name="uq_selection_reservations_run_id",
            ),
        )
    existing_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("selection_reservations")}
    if "idx_selection_reservations_active" not in existing_indexes:
        op.create_index(
            "idx_selection_reservations_active",
            "selection_reservations",
            ["case_id", "medium", "status"],
        )
    # §17: TTL index — status + expires_at backs both the active-slot scan and the
    # expiry sweep that reclaims leases from stuck/abandoned runs.
    if "idx_selection_reservations_ttl" not in existing_indexes:
        op.create_index(
            "idx_selection_reservations_ttl",
            "selection_reservations",
            ["status", "expires_at"],
        )


def downgrade() -> None:
    op.drop_index("idx_selection_reservations_ttl", table_name="selection_reservations")
    op.drop_index("idx_selection_reservations_active", table_name="selection_reservations")
    op.drop_table("selection_reservations")
