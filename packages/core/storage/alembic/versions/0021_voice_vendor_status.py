from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0021_voice_vendor_status"
down_revision = "0020_resv_active_slot"
branch_labels = None
depends_on = None

_TABLE = "voice_profiles"


def upgrade() -> None:
    """Add ``vendor`` + ``status`` to voice_profiles (idempotent inspect-then-add).

    ``vendor`` groups voices by provider in the multi-vendor library; backfilled
    from the provider_profile_id prefix (``minimax.tts.prod`` -> ``minimax``),
    leaving sandbox/unknown as '' so the UI buckets them under '未指定厂商'.
    ``status`` is the clone state machine — existing rows are all ``ready``.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if "vendor" not in columns:
        op.add_column(_TABLE, sa.Column("vendor", sa.String(), nullable=False, server_default=""))
        op.execute(
            f"""
            update {_TABLE}
            set vendor = split_part(provider_profile_id, '.', 1)
            where provider_profile_id is not null
              and split_part(provider_profile_id, '.', 1) not in ('', 'sandbox')
            """
        )
    if "status" not in columns:
        op.add_column(
            _TABLE, sa.Column("status", sa.String(), nullable=False, server_default="ready")
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if "status" in columns:
        op.drop_column(_TABLE, "status")
    if "vendor" in columns:
        op.drop_column(_TABLE, "vendor")
