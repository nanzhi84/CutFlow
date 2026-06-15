from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_finished_video_lipsync"
down_revision = "0010_publishing_copy_cover"
branch_labels = None
depends_on = None


# LipSync provider attribution (HeyGem-primary → VideoReTalk-fallback) on
# finished_videos. All nullable / server-defaulted so the add is safe on populated
# tables. The initial schema bootstraps via Base.metadata.create_all() (which already
# includes these columns on fresh DBs), so this migration is a no-op there and only
# applies on databases provisioned before the columns were added.
_FINISHED_VIDEO_COLUMNS = (
    ("lipsync_provider_id", sa.String(), True, None),
    ("lipsync_fallback_used", sa.Boolean(), False, sa.text("false")),
    ("lipsync_fallback_reason", sa.Text(), True, None),
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
    _add_missing_columns(bind, "finished_videos", _FINISHED_VIDEO_COLUMNS)


def downgrade() -> None:
    bind = op.get_bind()
    _drop_columns(bind, "finished_videos", _FINISHED_VIDEO_COLUMNS)
