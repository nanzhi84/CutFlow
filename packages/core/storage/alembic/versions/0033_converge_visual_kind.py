from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Issue #133: finish the visual-kind convergence started by 0026. Migration 0026
# rewrote the *historical* ``portrait`` / ``broll`` rows to ``video``, but the demo
# media seed (``packages/core/storage/repository.py`` -> ``seed.py``) kept emitting
# ``kind="portrait"`` / ``kind="broll"`` rows, so any DB bootstrapped *after* 0026
# (or whose API lifespan re-seeded the demo assets) re-introduced legacy rows behind
# 0026's back. This migration re-runs the same idempotent flip so those residual
# seed rows converge before the ``UploadKind``/``MediaAssetRecord.kind`` enum/Literal
# removal (#133) makes reading a ``portrait``/``broll`` row a hard validation error.
# The seed itself is fixed in the same change (now emits ``kind="video"``), so this
# is a one-off cleanup rather than a recurring need.
#
# Scope guard: touches ONLY the visual *asset kind* (``media_assets.kind``, a plain
# String column with no enum/check constraint). The selection *medium*
# (``selection_ledger.medium``, the A-roll/B-roll track role) is a separate concept
# and is intentionally left untouched.
revision = "0033_converge_visual_kind"
down_revision = "0032_voice_case_bindings"
branch_labels = None
depends_on = None

_TABLE = "media_assets"


def upgrade() -> None:
    """Re-converge any residual ``portrait`` / ``broll`` media assets onto ``video``.

    Each migrated row keeps its provenance via an appended ``legacy_kind:<old>`` tag
    (skipping rows that already carry it, so a re-run over a partially-migrated row is
    a no-op). PostgreSQL evaluates every ``SET`` RHS against the row's pre-update
    state, so ``'legacy_kind:' || kind`` captures the OLD kind even as ``kind`` is
    overwritten. Idempotent + PostgreSQL-only; a no-op elsewhere and on clean DBs.
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
            tags = case
                when 'legacy_kind:' || kind = any(coalesce(tags, '{{}}'::varchar[]))
                    then tags
                else array_append(coalesce(tags, '{{}}'::varchar[]), 'legacy_kind:' || kind)
            end
        where kind in ('portrait', 'broll')
        """
    )


def downgrade() -> None:
    """Best-effort restore from the ``legacy_kind:<x>`` provenance tag.

    Mirrors 0026's downgrade: rows carrying ``legacy_kind:portrait`` /
    ``legacy_kind:broll`` are reverted to that kind and the tag removed. Meaningful
    only alongside a code rollback that re-enables the dedicated portrait/broll asset
    kinds. PostgreSQL-only; a no-op elsewhere.
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
