from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_publishing_copy_cover"
down_revision = "0009_case_evolution_learning"
branch_labels = None
depends_on = None


# §28.1 publish-copy + cover + platform-payload fields on publish_batch_items.
# All nullable / server-defaulted so the add is safe on populated tables. The
# initial schema bootstraps via Base.metadata.create_all() (which already includes
# these columns on fresh DBs), so this migration is a no-op there and only applies
# on databases provisioned before the columns were added.
_PUBLISH_ITEM_COLUMNS = (
    ("publish_content", sa.Text(), True, sa.text("''")),
    ("cover_title", sa.String(), True, sa.text("''")),
    ("cover_subtitle", sa.String(), True, sa.text("''")),
    ("cover_artifact_id", sa.String(), True, None),
    ("tags", postgresql.ARRAY(sa.String()), True, None),
    ("location", sa.String(), True, None),
    ("account_group", sa.String(), True, None),
    ("scheduled_at", sa.DateTime(timezone=True), True, None),
)


def _add_missing_columns(bind, table: str, columns) -> None:
    existing = {col["name"] for col in sa.inspect(bind).get_columns(table)}
    for name, type_, nullable, server_default in columns:
        if name in existing:
            continue
        op.add_column(
            table,
            sa.Column(name, type_, nullable=nullable, server_default=server_default),
        )


def _drop_columns(bind, table: str, columns) -> None:
    existing = {col["name"] for col in sa.inspect(bind).get_columns(table)}
    for name, *_ in columns:
        if name in existing:
            op.drop_column(table, name)


def upgrade() -> None:
    bind = op.get_bind()
    _add_missing_columns(bind, "publish_batch_items", _PUBLISH_ITEM_COLUMNS)


def downgrade() -> None:
    bind = op.get_bind()
    _drop_columns(bind, "publish_batch_items", _PUBLISH_ITEM_COLUMNS)
