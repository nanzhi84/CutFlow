from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0022_drop_publish_hashtags"
down_revision = "0021_voice_vendor_status"
branch_labels = None
depends_on = None

_TABLE = "publish_packages"


def upgrade() -> None:
    """Strip the retired ``hashtags`` key from publish_packages.platform_defaults.

    ``PublishDefaults.hashtags`` was removed from the contract (superseded by
    ``tags``). Existing rows persisted the field via ``model_dump`` so their
    stored ``platform_defaults`` JSONB still carries a ``hashtags`` key; under
    ``extra="forbid"`` a later ``PublishDefaults.model_validate(row)`` would
    raise on every such row. Drop the key so stored rows validate against the
    slimmed contract. PostgreSQL-only (JSONB op); a no-op elsewhere (SQLite unit
    fixtures build their schema from metadata, never carrying the key).
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    op.execute(
        f"""
        update {_TABLE}
        set platform_defaults = platform_defaults - 'hashtags'
        where jsonb_exists(platform_defaults, 'hashtags')
        """
    )


def downgrade() -> None:
    """Re-add an empty ``hashtags`` list to rows lacking it (best-effort restore)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    op.execute(
        f"""
        update {_TABLE}
        set platform_defaults = jsonb_set(platform_defaults, '{{hashtags}}', '[]'::jsonb)
        where not jsonb_exists(platform_defaults, 'hashtags')
        """
    )
