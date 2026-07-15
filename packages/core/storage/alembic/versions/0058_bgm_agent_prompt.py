from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0058_bgm_agent_prompt"
down_revision = "0057_drop_provider_retry_policy"
branch_labels = None
depends_on = None

_TEMPLATE_ID = "prompt_bgm_agent"
_VERSION_ID = "prompt_bgm_agent_v1"
_BINDING_ID = "prompt_binding_prompt_bgm_agent"
_NODE_ID = "BgmAgentPlanning"
_CONTENT = """你是短视频背景音乐选择 Agent。你的唯一职责是从给定候选中选择一个与口播语气匹配、且不抢人声的 BGM。

口播脚本：
{script}

背景音乐候选（candidate_id、asset_id、segment_id、mood、scene_fit、script_fit、avoid_script、duration、volume）：
{bgm_candidates}

选择规则：
1. bgm_id 只能复制候选中的 candidate_id；没有合适候选或不应配乐时输出 null。
2. 结合 mood、scene_fit、script_fit 与 avoid_script 判断，禁止虚构 ID。
3. 字幕、强调、字体、颜色、坐标、时间线和音效不属于你的职责。
4. 只能输出 bgm_id 和 analysis，不要 Markdown 或额外字段。

{repair_feedback}

只输出如下 JSON：
{{"bgm_id": "bgm_001", "analysis": "选择理由"}}"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("prompt_templates") or not inspector.has_table("prompt_versions"):
        return
    bind.execute(
        sa.text(
            """
            insert into prompt_templates
                (id, name, purpose, variables_schema_ref, output_schema_ref, status,
                 schema_version, created_at, updated_at)
            values
                (:id, 'BGM Agent', 'prompt.bgm.agent',
                 cast(:variables_schema_ref as jsonb), cast(:output_schema_ref as jsonb),
                 'active', 'v1', now(), now())
            on conflict (id) do update
            set name = excluded.name,
                purpose = excluded.purpose,
                variables_schema_ref = excluded.variables_schema_ref,
                output_schema_ref = excluded.output_schema_ref,
                status = 'active',
                updated_at = now()
            """
        ),
        {
            "id": _TEMPLATE_ID,
            "variables_schema_ref": json.dumps({"schema_id": "prompt.bgm.agent.variables"}),
            "output_schema_ref": json.dumps({"schema_id": "prompt.bgm.output"}),
        },
    )
    bind.execute(
        sa.text(
            """
            insert into prompt_versions
                (id, prompt_template_id, content, status, changelog,
                 approved_at, published_at, schema_version, created_at, updated_at)
            values
                (:id, :template_id, :content, 'published',
                 'Publish BGM-only Agent prompt (#209).',
                 now(), now(), 'v1', now(), now())
            on conflict (id) do update
            set content = excluded.content,
                status = 'published',
                changelog = excluded.changelog,
                approved_at = coalesce(prompt_versions.approved_at, now()),
                published_at = coalesce(prompt_versions.published_at, now()),
                updated_at = now()
            where prompt_versions.prompt_template_id = excluded.prompt_template_id
            """
        ),
        {"id": _VERSION_ID, "template_id": _TEMPLATE_ID, "content": _CONTENT},
    )
    if inspector.has_table("prompt_bindings"):
        bind.execute(
            sa.text(
                """
                insert into prompt_bindings
                    (id, prompt_template_id, prompt_version_id, node_id,
                     priority, enabled, schema_version, created_at, updated_at)
                values
                    (:id, :template_id, :version_id, :node_id,
                     1, true, 'v1', now(), now())
                on conflict (id) do update
                set prompt_template_id = excluded.prompt_template_id,
                    prompt_version_id = excluded.prompt_version_id,
                    node_id = excluded.node_id,
                    priority = excluded.priority,
                    enabled = true,
                    updated_at = now()
                """
            ),
            {
                "id": _BINDING_ID,
                "template_id": _TEMPLATE_ID,
                "version_id": _VERSION_ID,
                "node_id": _NODE_ID,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("prompt_bindings"):
        bind.execute(sa.text("delete from prompt_bindings where id = :id"), {"id": _BINDING_ID})
    if inspector.has_table("prompt_versions"):
        bind.execute(sa.text("delete from prompt_versions where id = :id"), {"id": _VERSION_ID})
    if inspector.has_table("prompt_templates"):
        bind.execute(sa.text("delete from prompt_templates where id = :id"), {"id": _TEMPLATE_ID})
