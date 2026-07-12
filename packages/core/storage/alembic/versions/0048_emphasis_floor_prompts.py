from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0048_emphasis_floor_prompts"
down_revision = "0047_media_selection_diversity"
branch_labels = None
depends_on = None

# Emphasis floor (fix/caption-timing-huazi-floor): a finished video should carry at
# least 5 huazi events. This migration keeps existing DBs correct on a migrate-only
# deploy path, independent of whether/when ``seed_database`` runs:
#   * CreativeIntent must request 8-10 emphasis phrases (2-10 chars) so the pipeline
#     has candidate headroom after pixel-safety attrition;
#   * PostProcess must select 5-8 caption options when >=5 are offered (all when <5).
# Each update is guarded by a marker unique to the new content, so re-running is a
# no-op and an already-migrated row is never clobbered. ``seed_database`` performs the
# same content sync via ``_needs_prompt_version_sync``; this is the migrate-only twin.

_CREATIVE_INTENT_FLOOR_MARKER = "%至少给出 6 条%"
_POSTPROCESS_FLOOR_MARKER = "%必须选择 5 到 8 个事件的 caption option%"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _prompt_group_content(version_id: str) -> str:
    path = Path(__file__).resolve().parents[2] / "prompt_group_defaults.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("items", []):
        if item.get("version_id") == version_id:
            return str(item["content"])
    raise RuntimeError(f"Missing {version_id} in prompt_group_defaults.json")


def _creative_intent_content() -> str:
    # Mirrors packages/core/storage/repository.py::prompt_creative_intent_v1 content.
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
        "- emphasis：字符串数组，通常 8 到 10 条（脚本很短放不下时尽可能多给，至少给出 6 条），"
        "挑出最值得在画面上做整句强调（花字）的关键短语；"
        "每条必须逐字取自脚本原文（是脚本里的一段连续子串）、长度 2 到 10 个字；"
        "确实没有合适的才给空数组 []。\n\n"
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
            _CREATIVE_INTENT_FLOOR_MARKER,
            "Synced built-in CreativeIntent prompt emphasis floor contract.",
        ),
        (
            "prompt_postprocess_agent_v1",
            "prompt_postprocess_agent",
            _prompt_group_content("prompt_postprocess_agent_v1"),
            _POSTPROCESS_FLOOR_MARKER,
            "Synced built-in PostProcess prompt emphasis floor contract.",
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


def downgrade() -> None:
    return
