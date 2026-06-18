from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016_finished_video_number"
down_revision = "0015_publish_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("finished_videos")}
    if "video_number" not in columns:
        op.add_column("finished_videos", sa.Column("video_number", sa.String(), nullable=True))
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("finished_videos")}
    if "uq_finished_videos_case_video_number" not in constraints:
        op.create_unique_constraint(
            "uq_finished_videos_case_video_number",
            "finished_videos",
            ["case_id", "video_number"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("finished_videos")}
    if "uq_finished_videos_case_video_number" in constraints:
        op.drop_constraint("uq_finished_videos_case_video_number", "finished_videos", type_="unique")
    columns = {column["name"] for column in inspector.get_columns("finished_videos")}
    if "video_number" in columns:
        op.drop_column("finished_videos", "video_number")
