from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0064_caption_style_intensity"
down_revision = "0063_workflow_cancel_request"
branch_labels = None
depends_on = None

_CONTENT = """你是资深短视频创意策划。基于下面的口播脚本，提炼创意结构。

严格要求：直接输出一个 JSON 对象（以左花括号开头、右花括号结尾）；禁止使用 markdown 代码块；禁止任何前后缀说明文字。

JSON 必须且只能包含以下字段：
- hook：字符串，一句话开场钩子。
- tone：字符串，整体语气风格。
- audience：字符串，目标受众。
- beats：字符串数组，3 到 6 条，按顺序列出脚本的关键叙事节拍。
- bgm_mood：字符串，必须从 沉稳 / 温暖 / 轻快 / 励志 / 高能 / 紧张 / 高级 / 俏皮 中选择一个，用来指导背景音乐精确匹配；不要输出枚举外词。
- emphasis：对象数组，可为空 []。每项只能包含 phrase、priority、display_mode、intensity；phrase 必须逐字取自脚本原文连续子串；priority 为 0 到 100 整数；display_mode 只能是 inline 或 whole_cue。inline 表示句内局部强调，whole_cue 表示整条短 cue 在固定字幕带内强调；intensity 只能是 normal、strong 或 hero，单条视频 hero 最多 1 个、strong 最多 3 个，其余使用 normal；不要输出位置、坐标、字体或动画。

脚本：
{script}"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not sa.inspect(bind).has_table("prompt_versions"):
        return
    bind.execute(
        sa.text(
            """
            insert into prompt_versions
                (id, prompt_template_id, content, status, changelog,
                 approved_at, published_at, schema_version, created_at, updated_at)
            select
                'prompt_creative_intent_v2', 'prompt_creative_intent', :content,
                'published', 'Publish emphasis style intensity semantics (#216).',
                now(), now(), 'v1', now(), now()
            where exists (
                select 1 from prompt_templates
                where id = 'prompt_creative_intent'
            )
            on conflict (id) do nothing
            """
        ),
        {"content": _CONTENT},
    )
    if not sa.inspect(bind).has_table("prompt_bindings"):
        return
    bind.execute(
        sa.text(
            """
            insert into prompt_bindings
                (id, prompt_template_id, prompt_version_id, node_id, priority, enabled,
                 schema_version, created_at, updated_at)
            select
                'prompt_binding_global_intent', 'prompt_creative_intent',
                'prompt_creative_intent_v2', 'ResolveCreativeIntent', 1, true,
                'v1', now(), now()
            where exists (
                select 1 from prompt_versions
                where id = 'prompt_creative_intent_v2'
                  and prompt_template_id = 'prompt_creative_intent'
            )
            on conflict (id) do update
            set prompt_version_id = excluded.prompt_version_id,
                updated_at = now()
            where prompt_bindings.prompt_template_id = 'prompt_creative_intent'
              and prompt_bindings.node_id = 'ResolveCreativeIntent'
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not sa.inspect(bind).has_table("prompt_bindings"):
        return
    bind.execute(
        sa.text(
            """
            update prompt_bindings
            set prompt_version_id = 'prompt_creative_intent_v1',
                updated_at = now()
            where id = 'prompt_binding_global_intent'
              and prompt_template_id = 'prompt_creative_intent'
              and node_id = 'ResolveCreativeIntent'
              and prompt_version_id = 'prompt_creative_intent_v2'
            """
        )
    )
