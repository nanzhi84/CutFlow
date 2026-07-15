from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0056_media_prompt_domains"
down_revision = "0055_tts_async_icl2"
branch_labels = None
depends_on = None

_TEMPLATE_ID = "prompt_media_selection_agent"
_V1_VERSION_ID = "prompt_media_selection_agent_v1"
_V2_VERSION_ID = "prompt_media_selection_agent_v2"
_BINDING_ID = "prompt_binding_prompt_media_selection_agent"
_NODE_ID = "MediaSelectionAgentPlanning"
_CHANGELOG = "Publish slot-scoped, construction-safe media candidate domains."


def _v2_seed_item() -> dict:
    return {"template_id":"prompt_media_selection_agent","version_id":"prompt_media_selection_agent_v2","name":"Media Selection Agent v2","purpose":"prompt.media_selection.agent","source_key":"media_selection_agent_prompt_v2","variables_schema_id":"prompt.media_selection.agent.variables","output_schema_id":"prompt.media_selection.output","variable_hints":["script","title","edit_instruction","video_duration","max_broll_inserts","portrait_uniqueness_rule","broll_uniqueness_rule","narration_units","safe_cut_boundaries","portrait_slots","broll_slots","repair_feedback"],"content":"你是短视频媒体选择 Agent。你的唯一职责是从系统给定的 ID 中选择人像主轨与 B-roll；背景音乐、字幕、花字和任何视觉样式都由独立的后处理链路负责。\n\n口播脚本：\n{script}\n视频标题：{title}\n额外剪辑要求：\n{edit_instruction}\n目标时长：{video_duration} 秒\nB-roll 数量上限：{max_broll_inserts}\n\n旁白单元：\n{narration_units}\n安全切点：\n{safe_cut_boundaries}\n人像插槽：\n{portrait_slots}\nB-roll 插槽：\n{broll_slots}\n\n输入说明：\n- 每个 slot 的 legal_candidates 已内嵌 candidate_id、asset_id、可用时长和语义描述，不需要再查全局候选表。\n- legal_candidates 已同时满足 retrieval 与单素材容量约束；覆盖 witness 先从完整合法域求解，再做展示裁剪。人像域跨 slot 不复用 asset_id。insert 模式只有最多 max_broll_inserts 个互不重叠 slot 获得非空域，且不同非空 slot 的任意候选都不会重复 candidate_id、非空 asset_id 或非空 diversity_key；full_coverage 模式仅保证 candidate_id 跨 slot 唯一，允许复用 asset_id/diversity_key。\n- 人像硬约束：{portrait_uniqueness_rule}\n- B-roll 硬约束：{broll_uniqueness_rule}\n\n选择目标：\n1. portrait_plan 必须覆盖每一个 portrait slot；每个 slot 恰好输出一次，candidate_id 只能复制自该 slot.legal_candidates。\n2. candidate_id 只能复制自同一 slot.legal_candidates，一个 slot 最多一次，总数不得超过 {max_broll_inserts}。insert 模式只在画面明显更能解释旁白时选择；full_coverage 模式必须覆盖每个 B-roll slot。\n3. 若两个 B-roll slot 的 conflicts_with_slot_ids 互相指向，最多选择其中一个。\n4. 所有 slot_id、candidate_id 必须逐字来自输入，禁止凭印象从别的 slot 搬用 ID，禁止虚构。\n\n输出前必须机械自检：\n- portrait_plan 条数必须等于 portrait slot 数，每个 slot_id 恰好一次；\n- 每一组 slot_id + candidate_id 必须能在同一个输入 slot 中找到；\n- broll_plan 条数必须小于等于 {max_broll_inserts}，slot_id 不重复且不同时选择冲突 slot；full_coverage 模式还必须逐 slot 覆盖；\n- 只要任一检查不通过，先在内部改正，再输出最终 JSON；analysis 不得声称与实际选择矛盾的唯一性结论。\n\n严格职责边界：\n- 不得选择或输出 bgm_id、bgm_plan。\n- 不得选择或输出普通字幕、花字、caption、huazi、style、font、font_size、color、rect、position、coordinates、animation、sfx。\n- 不得输出 frame、start、end、duration 等时间或几何字段。\n- portrait_plan 与 broll_plan 的每个 choice 只允许 slot_id、candidate_id、reason；source_mode、confidence、matched_keywords 均由本地程序物化。\n- 只输出下面三个顶层字段，不要 Markdown 或额外说明。\n\n{repair_feedback}\n\n只输出如下 JSON：\n{{\n  \"portrait_plan\": [{{\"slot_id\": \"pwin_000\", \"candidate_id\": \"pc_000\", \"reason\": \"为什么选择\"}}],\n  \"broll_plan\": [{{\"slot_id\": \"bwin_002\", \"candidate_id\": \"bc_009\", \"reason\": \"为什么覆盖\"}}],\n  \"analysis\": \"媒体选择思路\"\n}}"}


def upgrade() -> None:
    """Publish v2 and atomically move the node binding without rewriting v1."""

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("prompt_templates") or not inspector.has_table("prompt_versions"):
        return

    item = _v2_seed_item()
    bind.execute(
        sa.text(
            """
            insert into prompt_templates
                (id, name, purpose, variables_schema_ref, output_schema_ref, status,
                 schema_version, created_at, updated_at)
            values
                (:id, :name, :purpose,
                 cast(:variables_schema_ref as jsonb), cast(:output_schema_ref as jsonb),
                 'active', 'v1', now(), now())
            on conflict (id) do nothing
            """
        ),
        {
            "id": _TEMPLATE_ID,
            "name": str(item["name"]),
            "purpose": str(item["purpose"]),
            "variables_schema_ref": json.dumps({"schema_id": item["variables_schema_id"]}),
            "output_schema_ref": json.dumps({"schema_id": item["output_schema_id"]}),
        },
    )
    bind.execute(
        sa.text(
            """
            insert into prompt_versions
                (id, prompt_template_id, content, status, changelog,
                 approved_at, published_at, schema_version, created_at, updated_at)
            values
                (:id, :template_id, :content, 'published', :changelog,
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
        {
            "id": _V2_VERSION_ID,
            "template_id": _TEMPLATE_ID,
            "content": str(item["content"]),
            "changelog": _CHANGELOG,
        },
    )
    if inspector.has_table("prompt_bindings"):
        bind.execute(
            sa.text(
                """
                insert into prompt_bindings
                    (id, prompt_template_id, prompt_version_id, node_id, priority, enabled,
                     schema_version, created_at, updated_at)
                values
                    (:id, :template_id, :version_id, :node_id, 1, true, 'v1', now(), now())
                on conflict (id) do update
                set prompt_template_id = excluded.prompt_template_id,
                    prompt_version_id = excluded.prompt_version_id,
                    node_id = excluded.node_id,
                    enabled = true,
                    updated_at = now()
                """
            ),
            {
                "id": _BINDING_ID,
                "template_id": _TEMPLATE_ID,
                "version_id": _V2_VERSION_ID,
                "node_id": _NODE_ID,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not sa.inspect(bind).has_table("prompt_bindings"):
        return
    bind.execute(
        sa.text(
            """
            update prompt_bindings
            set prompt_version_id = :version_id,
                updated_at = now()
            where id = :binding_id
              and prompt_template_id = :template_id
              and prompt_version_id = :v2_version_id
            """
        ),
        {
            "binding_id": _BINDING_ID,
            "template_id": _TEMPLATE_ID,
            "version_id": _V1_VERSION_ID,
            "v2_version_id": _V2_VERSION_ID,
        },
    )
