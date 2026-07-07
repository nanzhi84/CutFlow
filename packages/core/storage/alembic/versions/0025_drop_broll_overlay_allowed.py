from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit); the file name
# carries the fuller description, mirroring 0023/0024 (ids
# ``0023_drop_lipsync_adv_fields`` / ``0024_drop_out_strict_fields``).
revision = "0025_drop_broll_overlay"
down_revision = "0024_drop_out_strict_fields"
branch_labels = None
depends_on = None

_TABLE = "artifacts"
_KIND = "narration.units"
_KEY = "broll_overlay_allowed"


def upgrade() -> None:
    """Strip the un-consumed ``broll_overlay_allowed`` key from persisted narration units.

    ``NarrationUnit.broll_overlay_allowed`` was written by the narration builders
    (``end - start >= 0.18``) but never consumed: ``BrollPlanning`` /
    historical B-roll planners convert each ``NarrationUnit`` into a ``ScriptSegment``
    using only text/start/end/keywords, and real B-roll inserts are governed by
    ``plan_insertions()``'s host narration window + ``_MIN_INSERT_SECONDS`` (issue
    #100). The field is removed from the contract, but ``narration.units`` artifacts
    persisted the key inside ``payload->'units'[*]`` via ``model_dump`` (defaults
    included). B-roll planning re-reads those rows with
    ``NarrationUnit.model_validate(unit)`` (extra="forbid"), so a resumed legacy run
    would raise on every stored unit. Drop the key from each unit element so stored
    rows validate against the slimmed contract.

    This field lives inside a JSONB *array element*, not a top-level key, so we
    rebuild ``payload->'units'`` with the key removed from every element.
    PostgreSQL-only (JSONB op); a no-op elsewhere (SQLite unit fixtures build their
    schema from metadata and never carry these rows). Idempotent: the EXISTS guard
    skips rows that no longer carry the key on a second pass.
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
        set payload = jsonb_set(
            payload,
            '{{units}}',
            (
                select coalesce(jsonb_agg(elem - '{_KEY}'), '[]'::jsonb)
                from jsonb_array_elements(payload -> 'units') elem
            )
        )
        where kind = '{_KIND}'
          and jsonb_typeof(payload) = 'object'
          and jsonb_typeof(payload -> 'units') = 'array'
          and exists (
              select 1
              from jsonb_array_elements(payload -> 'units') e
              where e ? '{_KEY}'
          )
        """
    )


def downgrade() -> None:
    """Best-effort restore: re-seed ``broll_overlay_allowed`` with its old default.

    Each unit gets the key re-added with the contract's former default
    (``false``) only when absent; any value already present is preserved (the
    default block is concatenated on the left so the stored element wins).
    Meaningful only alongside a code rollback that restores the contract field.
    PostgreSQL-only; a no-op elsewhere.
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
        set payload = jsonb_set(
            payload,
            '{{units}}',
            (
                select coalesce(
                    jsonb_agg('{{"{_KEY}": false}}'::jsonb || elem),
                    '[]'::jsonb
                )
                from jsonb_array_elements(payload -> 'units') elem
            )
        )
        where kind = '{_KIND}'
          and jsonb_typeof(payload) = 'object'
          and jsonb_typeof(payload -> 'units') = 'array'
        """
    )
