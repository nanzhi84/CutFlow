from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0050_provider_result_payload"
down_revision = "0049_provider_idempotency"
branch_labels = None
depends_on = None

_TABLE = "provider_invocations"
_COLUMN = "result_payload"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return

    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, postgresql.JSONB(), nullable=True))

    # No back-fill: rows that succeeded before this column exists have no recoverable
    # result, so the Gateway keeps rejecting a re-run that hits them.


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return

    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
