from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0059_upload_normalized_state"
down_revision = "0058_resumable_uploads"
branch_labels = None
depends_on = None


def _has_normalized_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("upload_sessions"):
        return False
    return any(
        column["name"] == "normalized"
        for column in inspector.get_columns("upload_sessions")
    )


def upgrade() -> None:
    # 0001 creates current Base.metadata on clean installs. The guard also lets
    # environments that briefly applied the pre-0059 feature branch converge
    # without trying to add the column twice.
    if not _has_normalized_column():
        op.add_column(
            "upload_sessions",
            sa.Column(
                "normalized",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _has_normalized_column():
        op.drop_column("upload_sessions", "normalized")
