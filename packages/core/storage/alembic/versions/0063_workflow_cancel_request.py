from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0063_workflow_cancel_request"
down_revision = "0062_drop_v1_prompts"
branch_labels = None
depends_on = None

_TABLE = "workflow_runs"
_CONSTRAINT = "workflow_runs_cancel_mode"


def _existing_cancel_constraint(inspector) -> str | None:
    return next(
        (
            item["name"]
            for item in inspector.get_check_constraints(_TABLE)
            if item["name"] and item["name"].endswith(_CONSTRAINT)
        ),
        None,
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "cancel_mode" not in columns:
        op.add_column(_TABLE, sa.Column("cancel_mode", sa.String(), nullable=True))
    if "cancel_requested_at" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        )
    if _existing_cancel_constraint(inspector) is None:
        op.create_check_constraint(
            _CONSTRAINT,
            _TABLE,
            "cancel_mode IS NULL OR cancel_mode IN ('graceful', 'force')",
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    constraint = _existing_cancel_constraint(inspector)
    if constraint is not None:
        op.drop_constraint(op.f(constraint), _TABLE, type_="check")
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "cancel_requested_at" in columns:
        op.drop_column(_TABLE, "cancel_requested_at")
    if "cancel_mode" in columns:
        op.drop_column(_TABLE, "cancel_mode")
