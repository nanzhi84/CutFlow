from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0020_selection_reservation_active_slot"
down_revision = "0019_user_generation_defaults"
branch_labels = None
depends_on = None

_TABLE = "selection_reservations"
_INDEX = "uq_selection_reservations_active_slot"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return

    op.execute(
        f"""
        update {_TABLE}
        set status = 'expired',
            released_at = coalesce(released_at, now())
        where status = 'reserved'
          and expires_at <= now()
        """
    )
    op.execute(
        f"""
        with ranked as (
            select id,
                   row_number() over (
                       partition by case_id, medium, asset_id
                       order by
                           created_at desc,
                           id desc
                   ) as rn
            from {_TABLE}
            where status = 'reserved'
        )
        update {_TABLE} reservation
        set status = 'released',
            released_at = coalesce(reservation.released_at, now())
        from ranked
        where reservation.id = ranked.id
          and ranked.rn > 1
        """
    )

    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            _TABLE,
            ["case_id", "medium", "asset_id"],
            unique=True,
            postgresql_where=sa.text("status = 'reserved'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
