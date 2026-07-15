from __future__ import annotations


from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0051_broll_legal_candidates"
down_revision = "0050_provider_result_payload"
branch_labels = None
depends_on = None

_VERSION_ID = "prompt_media_selection_agent_v1"
_TEMPLATE_ID = "prompt_media_selection_agent"
_LEGAL_CANDIDATES_MARKER = "B-roll 插槽（candidate_id 必须来自对应 legal_candidate_ids"
_LEGACY_ALLOWED_SLOTS_MARKER = "allowed_slot_ids"


def _current_media_selection_prompt() -> str:
    return "你是短视频媒体选择 Agent。你的唯一职责是从系统给定的 ID 中选择人像主轨与 B-roll；背景音乐、字幕、花字和任何视觉样式都由独立的后处理链路负责。\n\n口播脚本：\n{script}\n视频标题：{title}\n额外剪辑要求：\n{edit_instruction}\n目标时长：{video_duration} 秒\nB-roll 数量上限：{max_broll_inserts}\n\n旁白单元：\n{narration_units}\n安全切点：\n{safe_cut_boundaries}\n人像插槽（candidate_id 必须来自对应 legal_candidate_ids）：\n{portrait_slots}\nB-roll 插槽（candidate_id 必须来自对应 legal_candidate_ids；该列表已同时满足 retrieval TopK 与单素材容量约束）：\n{broll_slots}\n人像候选（candidate_id | asset_id | available_seconds | description | reason）：\n{portrait_candidates}\nB-roll 候选（candidate_id | asset_id | diversity_key | scene_name | matched_keywords | available_seconds | description）：\n{broll_candidates}\n\n选择目标：\n1. portrait_plan 必须覆盖每个 portrait slot，只选对应 legal_candidate_ids 中的 candidate_id。{portrait_uniqueness_rule}\n2. broll_plan 只在画面能更好解释旁白时选择，且 candidate_id 必须来自对应 slot 的 legal_candidate_ids；数量不得超过上限，一个 slot 最多选择一个 candidate_id。{broll_uniqueness_rule}\n3. 所有 slot_id、candidate_id 必须来自输入，禁止虚构。\n\n严格职责边界：\n- 不得选择或输出 bgm_id、bgm_plan。\n- 不得选择或输出普通字幕、花字、caption、huazi、style、font、font_size、color、rect、position、coordinates、animation、sfx。\n- 不得输出 frame、start、end、duration 等时间或几何字段。\n- portrait_plan 与 broll_plan 的每个 choice 只允许 slot_id、candidate_id、reason；source_mode、confidence、matched_keywords 均由本地程序物化。\n- 只输出下面三个顶层字段，不要 Markdown 或额外说明。\n\n{repair_feedback}\n\n只输出如下 JSON：\n{{\n  \"portrait_plan\": [{{\"slot_id\": \"pslot_000\", \"candidate_id\": \"pc_000\", \"reason\": \"为什么选择\"}}],\n  \"broll_plan\": [{{\"slot_id\": \"bslot_002\", \"candidate_id\": \"bc_009\", \"reason\": \"为什么覆盖\"}}],\n  \"analysis\": \"媒体选择思路\"\n}}"


def upgrade() -> None:
    """Publish the slot-oriented B-roll legal-candidate prompt contract."""

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
                changelog = 'Aligned B-roll slots with legal candidate IDs.',
                approved_at = coalesce(approved_at, now()),
                published_at = coalesce(published_at, now()),
                updated_at = now()
            where id = :version_id
              and prompt_template_id = :template_id
              and (
                content not like :legal_candidates_marker
                or content like :legacy_allowed_slots_marker
              )
            """
        ),
        {
            "content": _current_media_selection_prompt(),
            "version_id": _VERSION_ID,
            "template_id": _TEMPLATE_ID,
            "legal_candidates_marker": f"%{_LEGAL_CANDIDATES_MARKER}%",
            "legacy_allowed_slots_marker": f"%{_LEGACY_ALLOWED_SLOTS_MARKER}%",
        },
    )


def downgrade() -> None:
    # Reintroducing retrieval-only allowed_slot_ids would weaken the prompt contract.
    return
