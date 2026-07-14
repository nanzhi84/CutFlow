"""Add finished_videos.cover_thumb_artifact (issue #206).

The Outputs list card used to render the full-size cover — a lossless 1080x1920
PNG of ~2.3 MB — as its thumbnail. This column holds an ArtifactRef to a small
WebP derivative so the card downloads ~30 KB instead. Nullable: rows exported
before this migration keep NULL and fall back to cover_artifact until
``scripts/backfill_cover_thumbnails.py`` fills them in.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0052_finished_video_cover_thumb"
down_revision = "0051_broll_legal_candidates"
branch_labels = None
depends_on = None

_TABLE = "finished_videos"
_COLUMN = "cover_thumb_artifact"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, JSONB(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
