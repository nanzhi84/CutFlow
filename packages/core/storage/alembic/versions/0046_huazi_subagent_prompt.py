from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0046_huazi_subagent_prompt"
down_revision = "0045_drop_subtitle_preset"
branch_labels = None
depends_on = None

# Caption Display v2 (issue #188): the active v2 workflow separates media-only
# selection from deterministic caption-window compilation and a final BGM / complete
# caption-option selection pass. This migration keeps existing DBs correct on a
# migrate-only deploy path, independent of whether/when ``seed_database`` runs:
#   * the stored ``prompt_editing_agent_v1`` content is re-synced to the
#     huazi-free version (guarded so only pre-#188 rows are touched);
#   * the legacy Huazi v1 prompt remains seeded for in-flight v1 runs;
#   * the v2 media-selection and postprocess prompts are inserted for the new nodes.
# ``seed_database`` also inserts missing seed rows on the next bootstrap / startup,
# so these inserts are an idempotent migrate-only safety net with the same stable ids.
_EDITING_VERSION_ID = "prompt_editing_agent_v1"
_EDITING_TEMPLATE_ID = "prompt_editing_agent"
_LEGACY_HUAZI_MARKER = "huazi_plan"

_PROMPT_SEEDS = (
    {
        "template_id": "prompt_huazi_subagent",
        "version_id": "prompt_huazi_subagent_v1",
        "binding_id": "prompt_binding_prompt_huazi_subagent",
        "node_id": "HuaziPlanningSubagent",
        "changelog": "Seed legacy HuaziPlanningSubagent prompt (#188).",
    },
    {
        "template_id": "prompt_media_selection_agent",
        "version_id": "prompt_media_selection_agent_v1",
        "binding_id": "prompt_binding_prompt_media_selection_agent",
        "node_id": "MediaSelectionAgentPlanning",
        "changelog": "Seed media-only selection Agent prompt (#188).",
    },
    {
        "template_id": "prompt_postprocess_agent",
        "version_id": "prompt_postprocess_agent_v1",
        "binding_id": "prompt_binding_prompt_postprocess_agent",
        "node_id": "PostProcessAgentPlanning",
        "changelog": "Seed postprocess BGM/caption-option Agent prompt (#188).",
    },
)


def _seed_item(version_id: str) -> dict:
    return {"prompt_editing_agent_v1":{"template_id":"prompt_editing_agent","version_id":"prompt_editing_agent_v1","name":"Editing Agent Default","purpose":"prompt.editing.agent","source_key":"editing_agent_prompt","variables_schema_id":"prompt.editing.agent.variables","output_schema_id":"prompt.editing.output","variable_hints":["script","title","edit_instruction","video_duration","max_broll_inserts","portrait_uniqueness_rule","narration_units","safe_cut_boundaries","portrait_slots","broll_slots","portrait_candidates","broll_candidates","bgm_candidates","repair_feedback"],"content":"你是一位统一短视频剪辑 Agent（剪辑导演）。你要像真人剪辑师一样，基于脚本、旁白时间线、系统已规划好的安全切点与剪辑窗口，以及带语义标注的素材候选，一次性给出人像主轨、B-roll 和背景音乐的综合选择。\n\n【你只做“选择 ID”，绝不输出帧号或秒数】\n系统已经把时间线切好帧、把安全窗口和候选都编好了号。你只能在给定的 slot 和 candidate 里做语义选择，并输出对应的 ID。任何精确到帧的时间线、字幕字体、字号、颜色、描边和自由坐标都由本地程序按固定样式包计算——你不要发明、不要输出 timeline/frame/秒数，也绝不虚构不存在的 ID。\n\n口播脚本：\n{script}\n视频标题：{title}\n本条视频的额外剪辑要求（为空则按通用最佳实践）：\n{edit_instruction}\n主视频目标时长：{video_duration} 秒\n最多允许的 B-roll 覆盖数：{max_broll_inserts}\n\n旁白单元（每句话的文本与时间）：\n{narration_units}\n安全切点（上游已融合脚本句尾、ASR 边界与真实音频气口）：\n{safe_cut_boundaries}\n人像主轨插槽（每个 slot 包含 required_frames/required_seconds 和 legal_window_ids；你必须只从该 slot 的 legal_window_ids 里选一个 window_id）：\n{portrait_slots}\nB-roll 可覆盖窗口（每个对应一句旁白；每个 B-roll slot 最多只能输出一条 candidate_id；full_coverage 窗口的候选 available_seconds 必须不短于 required_seconds；multi_clip_allowed 字段仅作为兼容提示，不表示可拼接）：\n{broll_slots}\n\n可选人像候选（行式文本；首行为列名：candidate_id | asset_id | available_seconds | description | reason；之后每行一个候选，按 | 分隔）：\n{portrait_candidates}\n可选 B-roll 候选（行式文本；首行为列名：candidate_id | asset_id | scene_name | allowed_slot_ids | matched_keywords | available_seconds | description；之后每行一个候选，按 | 分隔；allowed_slot_ids 和 matched_keywords 用逗号分隔）：\n{broll_candidates}\n可选背景音乐候选：\n{bgm_candidates}\n\n剪辑目标：\n1. 人像主轨要覆盖全部 portrait_slots：为每一个 slot 选一个 window_id（必须取自该 slot 的 legal_window_ids，而不只是全局高分候选）。\n2. 满足额外剪辑要求：例如“尽量用穿搭相近的人像”，优先选择风格相近但 asset_id 不同的候选。{portrait_uniqueness_rule}\n3. B-roll 只在“画面明显更能说明这句话”时才覆盖，贴合度一般时保留主讲；覆盖数不超过 max_broll_inserts。每个 B-roll slot 最多只能输出一条 candidate_id；full_coverage 时 max_broll_inserts 已等于窗口数，只从候选中选择语义更贴合且 available_seconds >= required_seconds 的单条素材。\n4. 背景音乐从 bgm_candidates 里选与视频语气匹配、不抢人声的；没有合适或关闭 BGM 时置 null。\n\n硬约束：\n1. 所有 slot_id / window_id / candidate_id / bgm_id 必须来自上面给定的候选，禁止虚构。\n2. portrait_plan 必须覆盖每一个 portrait_slot；每个选择的 window_id 必须出现在对应 portrait_slot.legal_window_ids 中。{portrait_uniqueness_rule}不要自行按 source_start/source_end 推断，也不要为了高 score 选择不在 legal_window_ids 里的候选。\n3. broll_plan 总数不超过 max_broll_inserts；每个 B-roll slot 最多只能输出一条 candidate_id。即使看到 multi_clip_allowed 字段，也不得用多条候选拼接一个 slot；full_coverage 的候选 available_seconds 必须覆盖该 slot 的 required_seconds；不得重复同一 candidate_id。\n4. 字幕、字体包、字号、颜色、描边、布局坐标以及花字（强调字幕）都不由你决定——花字由独立的花字编排环节处理。你不得输出 font_plan、font_id、font_name、font_size、emphasis_font_size、style_plan、huazi、x/y/position/coordinates 或任意时间字段。\n5. 只输出下面结构的 JSON，不要输出任何解释性前后缀，也不要用 markdown 代码块。\n\n{repair_feedback}\n\n只输出如下 JSON：\n{{\n  \"portrait_plan\": [{{\"slot_id\": \"pslot_000\", \"window_id\": \"pc_000\", \"source_mode\": \"lipsynced\", \"reason\": \"为什么选它\"}}],\n  \"broll_plan\": [{{\"slot_id\": \"bslot_002\", \"candidate_id\": \"bc_009\", \"reason\": \"为什么覆盖\", \"confidence\": 0.86, \"matched_keywords\": [\"施工前\"]}}],\n  \"bgm_plan\": {{\"bgm_id\": \"bgm_xxx\", \"reason\": \"为什么\"}},\n  \"analysis\": \"整体剪辑思路一句话\"\n}}"},"prompt_huazi_subagent_v1":{"template_id":"prompt_huazi_subagent","version_id":"prompt_huazi_subagent_v1","name":"Huazi Planning Subagent","purpose":"prompt.huazi.subagent","variables_schema_id":"prompt.huazi.variables","output_schema_id":"prompt.huazi.output","variable_hints":["script","track_summary","normal_caption_zone","candidate_events","layout_boxes","animation_candidates","animation_directions","repair_feedback"],"content":"你是短视频花字（强调字幕）编排助手。系统已经确定了正片的人像/B-roll 画面、普通字幕与时间线；你只负责给已经选好的关键短句配上花字的“出现位置”和“进入动画”，让它既醒目又不挡脸、不压普通字幕。\n\n【你只做“选择 ID”，绝不发明位置或时间】\n候选短句、每条短句的候选布局框、可用动画都已编好号。你只能在给定的 event_id、layout_box_id、animation_id 里做选择，并给一个 priority（重要度整数，越大越重要）。字体、字号、颜色、描边、坐标、出现/消失时间、音效都由本地程序按固定规则计算——你不要输出这些，也不要改写短句文案。\n\n正片脚本（帮助你理解语气与重点）：\n{script}\n\n时间线轨道概览（每段画面的类型与时间，帮助你避开脸部和已占用区域）：\n{track_summary}\n\n普通字幕安全区说明：\n{normal_caption_zone}\n\n候选花字事件（每条对应一句旁白里的关键短语；event_id 唯一，text 是要强调的短句，start/end 是它出现的时间）：\n{candidate_events}\n\n每条事件的候选布局框（layout_box_id 唯一；rect 是归一化画布坐标，collision_score 越低越安全；allowed_enter_directions 是该框允许的滑入方向）：\n{layout_boxes}\n\n可选进入动画：\n{animation_candidates}\n滑入类动画对应的进入方向（只有当所选框的 allowed_enter_directions 含该方向时，才能给该框用对应的滑入动画）：\n{animation_directions}\n\n编排目标：\n1. 只给真正值得强调的短句配花字；不确定就少放，宁缺毋滥。\n2. 为每个选择的 event_id 选一个 layout_box_id：优先 collision_score 低、远离画面主体（数字人脸部通常在画面中部）、且不与相邻花字挤在同一区域的框。\n3. 为每个选择挑一个 animation_id；若用滑入类动画，其进入方向必须在所选框的 allowed_enter_directions 内，否则改用 pop_in/fade_in。\n4. 给每个选择一个 priority 整数表示重要度（越大越重要），供本地在花字过密或强调动画 punch 过多时确定性取舍。\n\n硬约束：\n1. event_id / layout_box_id / animation_id 必须来自上面给定的候选，禁止虚构；每个 event_id 最多出现一次。\n2. 不得输出字体、字号、颜色、描边、坐标、rect、出现/消失时间、音效等字段，也不得改写短句文案（不要输出 text、phrase）。\n3. 只输出下面结构的 JSON，不要输出任何解释性前后缀，也不要用 markdown 代码块。\n\n{repair_feedback}\n\n只输出如下 JSON：\n{{\n  \"huazi\": [{{\"event_id\": \"hz_001\", \"layout_box_id\": \"upper_center_medium\", \"animation_id\": \"pop_in\", \"priority\": 3, \"reason\": \"为什么这样编排\"}}]\n}}"},"prompt_media_selection_agent_v1":{"template_id":"prompt_media_selection_agent","version_id":"prompt_media_selection_agent_v1","name":"Media Selection Agent","purpose":"prompt.media_selection.agent","source_key":"media_selection_agent_prompt","variables_schema_id":"prompt.media_selection.agent.variables","output_schema_id":"prompt.media_selection.output","variable_hints":["script","title","edit_instruction","video_duration","max_broll_inserts","portrait_uniqueness_rule","broll_uniqueness_rule","narration_units","safe_cut_boundaries","portrait_slots","broll_slots","portrait_candidates","broll_candidates","repair_feedback"],"content":"你是短视频媒体选择 Agent。你的唯一职责是从系统给定的 ID 中选择人像主轨与 B-roll；背景音乐、字幕、花字和任何视觉样式都由独立的后处理链路负责。\n\n口播脚本：\n{script}\n视频标题：{title}\n额外剪辑要求：\n{edit_instruction}\n目标时长：{video_duration} 秒\nB-roll 数量上限：{max_broll_inserts}\n\n旁白单元：\n{narration_units}\n安全切点：\n{safe_cut_boundaries}\n人像插槽（candidate_id 必须来自对应 legal_candidate_ids）：\n{portrait_slots}\nB-roll 插槽（candidate_id 必须来自对应 legal_candidate_ids；该列表已同时满足 retrieval TopK 与单素材容量约束）：\n{broll_slots}\n人像候选（candidate_id | asset_id | available_seconds | description | reason）：\n{portrait_candidates}\nB-roll 候选（candidate_id | asset_id | diversity_key | scene_name | matched_keywords | available_seconds | description）：\n{broll_candidates}\n\n选择目标：\n1. portrait_plan 必须覆盖每个 portrait slot，只选对应 legal_candidate_ids 中的 candidate_id。{portrait_uniqueness_rule}\n2. broll_plan 只在画面能更好解释旁白时选择，且 candidate_id 必须来自对应 slot 的 legal_candidate_ids；数量不得超过上限，一个 slot 最多选择一个 candidate_id。{broll_uniqueness_rule}\n3. 所有 slot_id、candidate_id 必须来自输入，禁止虚构。\n\n严格职责边界：\n- 不得选择或输出 bgm_id、bgm_plan。\n- 不得选择或输出普通字幕、花字、caption、huazi、style、font、font_size、color、rect、position、coordinates、animation、sfx。\n- 不得输出 frame、start、end、duration 等时间或几何字段。\n- portrait_plan 与 broll_plan 的每个 choice 只允许 slot_id、candidate_id、reason；source_mode、confidence、matched_keywords 均由本地程序物化。\n- 只输出下面三个顶层字段，不要 Markdown 或额外说明。\n\n{repair_feedback}\n\n只输出如下 JSON：\n{{\n  \"portrait_plan\": [{{\"slot_id\": \"pslot_000\", \"candidate_id\": \"pc_000\", \"reason\": \"为什么选择\"}}],\n  \"broll_plan\": [{{\"slot_id\": \"bslot_002\", \"candidate_id\": \"bc_009\", \"reason\": \"为什么覆盖\"}}],\n  \"analysis\": \"媒体选择思路\"\n}}"},"prompt_postprocess_agent_v1":{"template_id":"prompt_postprocess_agent","version_id":"prompt_postprocess_agent_v1","name":"PostProcess Agent","purpose":"prompt.postprocess.agent","source_key":"postprocess_agent_prompt","variables_schema_id":"prompt.postprocess.agent.variables","output_schema_id":"prompt.postprocess.output","variable_hints":["script","bgm_candidates","caption_windows","caption_constraints","repair_feedback"],"content":"你是短视频后处理语义排序与样式选择 Agent。媒体时间线已经完成；你只负责一次性选择背景音乐 ID，并为每个强调字幕事件给出语义优先级和一个完整 caption_option_id。候选选项已经由本地程序完成帧时间、避脸、避场景文字、版式、字体和动画安全校验。最终数量、时间冲突、最小间隔和 hero 上限全部由本地求解器负责。\n\n口播脚本：\n{script}\n背景音乐候选：\n{bgm_candidates}\n强调字幕窗口与完整选项（event_id 下只能选择该事件给出的 caption_option_id）：\n{caption_windows}\n本地已知冲突与最大可行数量（只供理解，不要自行删事件）：\n{caption_constraints}\n\n选择目标：\n1. bgm_id 只能来自背景音乐候选；没有合适音乐时必须显式输出 null。\n2. caption_windows 中每个 event_id 都输出且只输出一条 caption_choice；priority 取 0-100，表示语义上值得强调的程度，越高越重要。\n3. 为每个事件选择最合适的完整 caption_option_id；event_id 与 caption_option_id 必须原样来自同一个输入事件。\n4. 不要因为事件互相冲突而漏掉其中任何一个。你只做语义排序和样式选择，本地求解器负责最终数量、时间冲突和 hero 上限。\n\n严格职责边界：\n- 只能选择 ID 和填写 priority/reason，不得输出或改写 text、font、font_id、font_size、font_weight、color、outline、start、end、time、frame、rect、x、y、position、coordinates、anchor_id、animation_id、sfx。\n- 不得输出 portrait_plan、broll_plan、media、timeline 或任何任意扩展字段。\n- 三个顶层字段 bgm_id、caption_choices、analysis 必须全部显式存在；字段名 captions 无效。\n- 只输出 JSON，不要 Markdown 或额外说明。\n\n{repair_feedback}\n\n只输出如下 JSON：\n{{\n  \"bgm_id\": \"bgm_xxx\",\n  \"caption_choices\": [{{\"event_id\": \"hz_001\", \"caption_option_id\": \"caption_option_001\", \"priority\": 80, \"reason\": \"为什么选择\"}}],\n  \"analysis\": \"后处理整体选择思路\"\n}}"}}[version_id]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("prompt_versions") or not inspector.has_table("prompt_templates"):
        return

    editing_item = _seed_item(_EDITING_VERSION_ID)

    # 1. Re-sync the main editing agent prompt to the huazi-free content. The
    #    guard (still contains the legacy "huazi_plan" token) makes this a no-op
    #    once applied and never clobbers an already-migrated row.
    bind.execute(
        sa.text(
            """
            update prompt_versions
            set content = :content,
                status = 'published',
                changelog = 'Removed huazi planning from EditingAgentPlanning prompt (#188).',
                approved_at = coalesce(approved_at, now()),
                published_at = coalesce(published_at, now()),
                updated_at = now()
            where id = :version_id
              and prompt_template_id = :template_id
              and content like :legacy_marker
            """
        ),
        {
            "content": str(editing_item["content"]),
            "version_id": _EDITING_VERSION_ID,
            "template_id": _EDITING_TEMPLATE_ID,
            "legacy_marker": f"%{_LEGACY_HUAZI_MARKER}%",
        },
    )

    # 2. Seed the legacy-v1 and active-v2 prompt rows if absent.
    for seed in _PROMPT_SEEDS:
        item = _seed_item(str(seed["version_id"]))
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
                "id": seed["template_id"],
                "name": str(item["name"]),
                "purpose": str(item["purpose"]),
                "variables_schema_ref": json.dumps(
                    {"schema_id": item["variables_schema_id"]}
                ),
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
                on conflict (id) do nothing
                """
            ),
            {
                "id": seed["version_id"],
                "template_id": seed["template_id"],
                "content": str(item["content"]),
                "changelog": seed["changelog"],
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
                    on conflict (id) do nothing
                    """
                ),
                {
                    "id": seed["binding_id"],
                    "template_id": seed["template_id"],
                    "version_id": seed["version_id"],
                    "node_id": seed["node_id"],
                },
            )


def downgrade() -> None:
    return
