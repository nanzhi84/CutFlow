from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0035_drop_portrait_reuse_warn"
down_revision = "0034_dashscope_unbounded_qwen37"
branch_labels = None
depends_on = None

_LEGACY_CODE = "portrait.asset_reuse_relaxed"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _has_table("node_runs"):
        return

    bind.execute(
        sa.text(
            """
            update node_runs
            set warnings = array_remove(warnings, :legacy_code),
                updated_at = now()
            where :legacy_code = any(warnings)
            """
        ),
        {"legacy_code": _LEGACY_CODE},
    )
    bind.execute(
        sa.text(
            """
            with cleaned as (
                select
                    node_runs.id,
                    coalesce(
                        jsonb_agg(item.value order by item.ordinality)
                            filter (where item.value->>'code' is distinct from :legacy_code),
                        '[]'::jsonb
                    ) as degradations
                from node_runs
                cross join lateral jsonb_array_elements(
                    coalesce(node_runs.degradations, '[]'::jsonb)
                ) with ordinality as item(value, ordinality)
                where node_runs.degradations @> cast(:legacy_degradation as jsonb)
                group by node_runs.id
            )
            update node_runs
            set degradations = cleaned.degradations,
                updated_at = now()
            from cleaned
            where node_runs.id = cleaned.id
            """
        ),
        {
            "legacy_code": _LEGACY_CODE,
            "legacy_degradation": f'[{{"code": "{_LEGACY_CODE}"}}]',
        },
    )


def downgrade() -> None:
    # No safe downgrade: the legacy warning code was intentionally removed from
    # the contract, so restoring persisted rows would make them unreadable again.
    return
