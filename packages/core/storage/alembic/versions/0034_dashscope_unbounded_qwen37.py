from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Keep existing deployments aligned with the provider seed/code defaults:
# - DashScope LLM moves from qwen-plus to qwen3.7-plus.
# - Provider profiles stop carrying max_tokens so DashScope calls are unbounded.
revision = "0034_dashscope_unbounded_qwen37"
down_revision = "0033_converge_visual_kind"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if _has_table("provider_profiles"):
        op.execute(
            """
            update provider_profiles
            set model_id = 'qwen3.7-plus',
                updated_at = now()
            where provider_id = 'dashscope.llm'
              and model_id = 'qwen-plus'
            """
        )
        if bind.dialect.name == "postgresql":
            op.execute(
                """
                update provider_profiles
                set default_options = default_options - 'max_tokens',
                    updated_at = now()
                where default_options ? 'max_tokens'
                """
            )
    if _has_table("provider_price_items"):
        op.execute(
            """
            update provider_price_items
            set model_id = 'qwen3.7-plus'
            where provider_id = 'dashscope.llm'
              and model_id = 'qwen-plus'
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if _has_table("provider_profiles"):
        op.execute(
            """
            update provider_profiles
            set model_id = 'qwen-plus',
                updated_at = now()
            where provider_id = 'dashscope.llm'
              and model_id = 'qwen3.7-plus'
            """
        )
    if _has_table("provider_price_items"):
        op.execute(
            """
            update provider_price_items
            set model_id = 'qwen-plus'
            where provider_id = 'dashscope.llm'
              and model_id = 'qwen3.7-plus'
            """
        )
