from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Issue #99: converge the visual asset kinds (``portrait`` / ``broll``) onto the
# unified ``video`` bucket. New uploads already normalize to ``kind=video`` (see
# ``apps/api/services/uploads.py``); this migration rewrites the *historical*
# rows so the whole library reads as one bucket and AnnotationV4 does the
# per-clip A-roll/B-roll classification. The original kind is preserved as a
# ``legacy_kind:<x>`` tag for traceability / a possible rollback.
#
# Scope guard: this touches ONLY the visual *asset kind* (``media_assets.kind``,
# a plain String column with no enum/check constraint). The selection *medium*
# (``selection_ledger.medium``, the A-roll/B-roll track role) is a separate
# concept and is intentionally left untouched.
revision = "0026_visual_kind_video"
down_revision = "0025_drop_broll_overlay"
branch_labels = None
depends_on = None

_TABLE = "media_assets"


def upgrade() -> None:
    """Rewrite historical ``portrait`` / ``broll`` media assets to ``video``.

    Each migrated row keeps its provenance via an appended ``legacy_kind:<old>``
    tag. PostgreSQL evaluates every ``SET`` RHS against the row's pre-update
    state, so ``'legacy_kind:' || kind`` captures the OLD kind even though
    ``kind`` is being overwritten in the same statement. The ``where`` clause
    makes the migration idempotent (a re-run finds no ``portrait``/``broll``
    rows). PostgreSQL-only; a no-op elsewhere.
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
        set kind = 'video',
            tags = array_append(coalesce(tags, '{{}}'::varchar[]), 'legacy_kind:' || kind)
        where kind in ('portrait', 'broll')
        """
    )


def downgrade() -> None:
    """Best-effort restore from the ``legacy_kind:<x>`` provenance tag.

    Rows carrying ``legacy_kind:portrait`` / ``legacy_kind:broll`` are reverted to
    that kind and the tag removed. Rows that were natively ``video`` (no legacy
    tag) are left alone. Meaningful only alongside a code rollback that re-enables
    the dedicated portrait/broll asset kinds. PostgreSQL-only; a no-op elsewhere.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    for legacy_kind in ("portrait", "broll"):
        op.execute(
            f"""
            update {_TABLE}
            set kind = '{legacy_kind}',
                tags = array_remove(tags, 'legacy_kind:{legacy_kind}')
            where kind = 'video'
              and 'legacy_kind:{legacy_kind}' = any(tags)
            """
        )
