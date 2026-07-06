from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0040_dashscope_llm_timeout"
down_revision = "0039_sync_edit_prompt_v2"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if _has_table("provider_profiles"):
        bind.execute(
            sa.text(
                """
                update provider_profiles
                set timeout_sec = 360,
                    updated_at = now()
                where id = 'dashscope.llm.prod'
                  and timeout_sec < 360
                """
            )
        )
    if _has_table("provider_capabilities"):
        bind.execute(
            sa.text(
                """
                update provider_capabilities
                set default_timeout_sec = 360,
                    updated_at = now()
                where id = 'cap_dashscope_llm_prod'
                  and default_timeout_sec < 360
                """
            )
        )


def downgrade() -> None:
    # Keep longer LLM timeout; lowering it can reintroduce provider timeouts.
    return
