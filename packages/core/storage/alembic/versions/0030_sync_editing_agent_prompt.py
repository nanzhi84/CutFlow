from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0030_sync_editing_agent_prompt"
down_revision = "0029_sync_editing_agent_prompt"
branch_labels = None
depends_on = None

_VERSION_ID = "prompt_editing_agent_v1"
_TEMPLATE_ID = "prompt_editing_agent"


def _current_editing_agent_prompt() -> str:
    return "你是一位统一短视频剪辑 Agent（剪辑导演）。你要像真人剪辑师一样，基于脚本、旁白时间线、系统已规划好的安全切点与剪辑窗口，以及带语义标注的素材候选，一次性给出人像主轨、B-roll 和背景音乐的综合选择。\n\n【你只做“选择 ID”，绝不输出帧号或秒数】\n系统已经把时间线切好帧、把安全窗口和候选都编好了号。你只能在给定的 slot 和 candidate 里做语义选择，并输出对应的 ID。任何精确到帧的时间线、字幕字体、字号、颜色、描边和自由坐标都由本地程序按固定样式包计算——你不要发明、不要输出 timeline/frame/秒数，也绝不虚构不存在的 ID。\n\n口播脚本：\n{script}\n视频标题：{title}\n本条视频的额外剪辑要求（为空则按通用最佳实践）：\n{edit_instruction}\n主视频目标时长：{video_duration} 秒\n最多允许的 B-roll 覆盖数：{max_broll_inserts}\n\n旁白单元（每句话的文本与时间）：\n{narration_units}\n安全切点（上游已融合脚本句尾、ASR 边界与真实音频气口）：\n{safe_cut_boundaries}\n人像主轨插槽（每个 slot 包含 required_frames/required_seconds 和 legal_window_ids；你必须只从该 slot 的 legal_window_ids 里选一个 window_id）：\n{portrait_slots}\nB-roll 可覆盖窗口（每个对应一句旁白；每个 B-roll slot 最多只能输出一条 candidate_id；full_coverage 窗口的候选 available_seconds 必须不短于 required_seconds；multi_clip_allowed 字段仅作为兼容提示，不表示可拼接）：\n{broll_slots}\n\n可选人像候选（行式文本；首行为列名：candidate_id | asset_id | available_seconds | description | reason；之后每行一个候选，按 | 分隔）：\n{portrait_candidates}\n可选 B-roll 候选（行式文本；首行为列名：candidate_id | asset_id | scene_name | allowed_slot_ids | matched_keywords | available_seconds | description；之后每行一个候选，按 | 分隔；allowed_slot_ids 和 matched_keywords 用逗号分隔）：\n{broll_candidates}\n可选背景音乐候选：\n{bgm_candidates}\n\n剪辑目标：\n1. 人像主轨要覆盖全部 portrait_slots：为每一个 slot 选一个 window_id（必须取自该 slot 的 legal_window_ids，而不只是全局高分候选）。\n2. 满足额外剪辑要求：例如“尽量用穿搭相近的人像”，优先选择风格相近但 asset_id 不同的候选。{portrait_uniqueness_rule}\n3. B-roll 只在“画面明显更能说明这句话”时才覆盖，贴合度一般时保留主讲；覆盖数不超过 max_broll_inserts。每个 B-roll slot 最多只能输出一条 candidate_id；full_coverage 时 max_broll_inserts 已等于窗口数，只从候选中选择语义更贴合且 available_seconds >= required_seconds 的单条素材。\n4. 背景音乐从 bgm_candidates 里选与视频语气匹配、不抢人声的；没有合适或关闭 BGM 时置 null。\n\n硬约束：\n1. 所有 slot_id / window_id / candidate_id / bgm_id 必须来自上面给定的候选，禁止虚构。\n2. portrait_plan 必须覆盖每一个 portrait_slot；每个选择的 window_id 必须出现在对应 portrait_slot.legal_window_ids 中。{portrait_uniqueness_rule}不要自行按 source_start/source_end 推断，也不要为了高 score 选择不在 legal_window_ids 里的候选。\n3. broll_plan 总数不超过 max_broll_inserts；每个 B-roll slot 最多只能输出一条 candidate_id。即使看到 multi_clip_allowed 字段，也不得用多条候选拼接一个 slot；full_coverage 的候选 available_seconds 必须覆盖该 slot 的 required_seconds；不得重复同一 candidate_id。\n4. 字幕、字体包、字号、颜色、描边、布局坐标以及花字（强调字幕）都不由你决定——花字由独立的花字编排环节处理。你不得输出 font_plan、font_id、font_name、font_size、emphasis_font_size、style_plan、huazi、x/y/position/coordinates 或任意时间字段。\n5. 只输出下面结构的 JSON，不要输出任何解释性前后缀，也不要用 markdown 代码块。\n\n{repair_feedback}\n\n只输出如下 JSON：\n{{\n  \"portrait_plan\": [{{\"slot_id\": \"pslot_000\", \"window_id\": \"pc_000\", \"source_mode\": \"lipsynced\", \"reason\": \"为什么选它\"}}],\n  \"broll_plan\": [{{\"slot_id\": \"bslot_002\", \"candidate_id\": \"bc_009\", \"reason\": \"为什么覆盖\", \"confidence\": 0.86, \"matched_keywords\": [\"施工前\"]}}],\n  \"bgm_plan\": {{\"bgm_id\": \"bgm_xxx\", \"reason\": \"为什么\"}},\n  \"analysis\": \"整体剪辑思路一句话\"\n}}"


def upgrade() -> None:
    """Re-sync the built-in EditingAgentPlanning prompt after 0029.

    0029 only repaired DBs still holding the pre-#136 legacy prompt. This second
    sync widens the stale-detection so DBs already on the #136 (or interim
    hardened) prompt also pick up the strict portrait-uniqueness placeholder:
    any row missing ``{portrait_uniqueness_rule}`` (or the hardening markers) is
    refreshed from prompt_group_defaults.json.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("prompt_versions"):
        return
    content = _current_editing_agent_prompt()
    bind.execute(
        sa.text(
            """
            update prompt_versions
            set content = :content,
                status = 'published',
                changelog = 'Synced built-in EditingAgentPlanning prompt contract.',
                approved_at = coalesce(approved_at, now()),
                published_at = coalesce(published_at, now()),
                updated_at = now()
            where id = :version_id
              and prompt_template_id = :template_id
              and (
                content like '%{asr_segments}%'
                or content like '%{portrait_slot_plan}%'
                or content like '%{portrait_requirement_groups}%'
                or content like '%{portrait_draft_plan}%'
                or content like '%"broll_overrides"%'
                or content like '%"subtitle_style_plan"%'
                or content not like '%legal_window_ids%'
                or content not like '%available_frames%'
                or content like '%允许重复使用同一素材%'
                or content not like '%{portrait_uniqueness_rule}%'
              )
            """
        ),
        {"content": content, "version_id": _VERSION_ID, "template_id": _TEMPLATE_ID},
    )


def downgrade() -> None:
    # No safe downgrade: the legacy prompt was incompatible with the current
    # EditingAgentPlanning node and is not reconstructed.
    return
