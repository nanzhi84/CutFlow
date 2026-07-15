from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0060_creative_intent_runs"
down_revision = "0059_bgm_agent_prompt"
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
- emphasis：对象数组，可为空 []。每项只能包含 phrase、priority、display_mode；phrase 必须逐字取自脚本原文连续子串；priority 为 0 到 100 整数；display_mode 只能是 inline 或 whole_cue。inline 表示句内局部强调，whole_cue 表示整条短 cue 在固定字幕带内强调；不要输出位置、坐标、字体或动画。

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
            update prompt_versions
            set content = :content,
                status = 'published',
                changelog = 'Publish inline caption Run emphasis semantics (#209).',
                approved_at = coalesce(approved_at, now()),
                published_at = coalesce(published_at, now()),
                updated_at = now()
            where id = 'prompt_creative_intent_v1'
              and prompt_template_id = 'prompt_creative_intent'
              and content not like '%display_mode%'
            """
        ),
        {"content": _CONTENT},
    )


def downgrade() -> None:
    return
