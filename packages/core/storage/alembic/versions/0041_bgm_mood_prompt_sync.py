from __future__ import annotations


from alembic import op
import sqlalchemy as sa

revision = "0041_bgm_mood_prompt_sync"
down_revision = "0040_dashscope_llm_timeout"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _prompt_group_content(version_id: str) -> str:
    return {"prompt_window_query_v1":"你是短视频素材检索 query 规划助手。你的任务是把已经确定好的时间线窗口转成向量检索意图文本，面向 qwen3-vl 视频语义检索。\n\n【输入】\n口播脚本：\n{script}\n\n剪辑指令：\n{edit_instruction}\n\n案例上下文（product / audience / tone）：\n{case_context}\n\n创意节拍：\n{creative_beats}\n\n窗口 JSON（每项含 window_id、kind、narration_text、scene_hint）：\n{windows}\n\n【任务】\n1. 为每个窗口生成一条中文为主的向量检索 query。\n2. query 优先写具象视觉要素：场景、动作、物件、人物外观、空间状态、画面证据。\n3. 若 scene_hint 非空，必须把它视为该窗口的视觉检索场景线索，并与 narration_text 融合；不要照抄成空泛标签。\n4. 融入剪辑指令里的偏好约束，但不要发明输入里没有的品牌、价格、地点或人物身份。\n5. kind=portrait 时，侧重口播人像、人物外观一致性、正脸/稳定出镜/口型可用等诉求。\n6. kind=broll 时，侧重能证明这句旁白的具象画面证据、过程、产品或场景细节。\n7. 每条 retrieval_intent 不超过 300 字。\n\n【硬约束】\n- 你只生成检索意图文本，不输出帧号、秒数、素材 ID、候选 ID 或剪辑决策。\n- 必须覆盖输入 windows 中的每个 window_id，window_id 必须原样返回。\n- 只输出 JSON 对象，不要 Markdown，不要代码块，不要解释。\n\n输出格式：\n{{\"window_queries\": [{{\"window_id\": \"...\", \"retrieval_intent\": \"...\"}}]}}"}[version_id]


def _creative_intent_content() -> str:
    return (
        "你是资深短视频创意策划。基于下面的口播脚本，提炼创意结构。\n\n"
        "严格要求：直接输出一个 JSON 对象（以左花括号开头、右花括号结尾）；"
        "禁止使用 markdown 代码块；禁止任何前后缀说明文字。\n\n"
        "JSON 必须且只能包含以下字段：\n"
        "- hook：字符串，一句话开场钩子。\n"
        "- tone：字符串，整体语气风格。\n"
        "- audience：字符串，目标受众。\n"
        "- beats：字符串数组，3 到 6 条，按顺序列出脚本的关键叙事节拍。\n"
        "- bgm_mood：字符串，必须从 沉稳 / 温暖 / 轻快 / 励志 / 高能 / 紧张 / 高级 / "
        "俏皮 中选择一个，用来指导背景音乐精确匹配；不要输出枚举外词。\n"
        "- emphasis：字符串数组，最多 6 条，挑出最值得在画面上做整句强调（花字）的关键短语；"
        "每条必须逐字取自脚本原文（是脚本里的一段连续子串）、长度 2 到 30 字；"
        "没有合适的就给空数组 []。\n\n"
        "脚本：\n"
        "{script}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _has_table("prompt_versions"):
        return

    prompts = [
        (
            "prompt_creative_intent_v1",
            "prompt_creative_intent",
            _creative_intent_content(),
            "%bgm_mood%",
            "Synced built-in CreativeIntent prompt BGM mood contract.",
        ),
        (
            "prompt_window_query_v1",
            "prompt_window_query",
            _prompt_group_content("prompt_window_query_v1"),
            "%scene_hint%",
            "Synced built-in WindowQueryPlanning prompt scene-hint contract.",
        ),
    ]
    for version_id, template_id, content, marker, changelog in prompts:
        bind.execute(
            sa.text(
                """
                update prompt_versions
                set content = :content,
                    status = 'published',
                    changelog = :changelog,
                    approved_at = coalesce(approved_at, now()),
                    published_at = coalesce(published_at, now()),
                    updated_at = now()
                where id = :version_id
                  and prompt_template_id = :template_id
                  and content not like :marker
                """
            ),
            {
                "content": content,
                "changelog": changelog,
                "version_id": version_id,
                "template_id": template_id,
                "marker": marker,
            },
        )

    if _has_table("prompt_bindings"):
        bind.execute(
            sa.text(
                """
                update prompt_bindings
                set prompt_version_id = case id
                        when 'prompt_binding_global_intent' then 'prompt_creative_intent_v1'
                        when 'prompt_binding_prompt_window_query' then 'prompt_window_query_v1'
                    end,
                    updated_at = now()
                where id in ('prompt_binding_global_intent', 'prompt_binding_prompt_window_query')
                """
            )
        )


def downgrade() -> None:
    return
