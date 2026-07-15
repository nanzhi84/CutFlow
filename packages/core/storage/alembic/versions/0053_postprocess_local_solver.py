from __future__ import annotations


from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0053_postprocess_local_solver"
down_revision = "0052_finished_video_cover_thumb"
branch_labels = None
depends_on = None

_VERSION_ID = "prompt_postprocess_agent_v1"
_TEMPLATE_ID = "prompt_postprocess_agent"
_LOCAL_SOLVER_MARKER = "本地求解器负责最终数量、时间冲突和 hero 上限"


def _current_postprocess_prompt() -> str:
    return "你是短视频后处理语义排序与样式选择 Agent。媒体时间线已经完成；你只负责一次性选择背景音乐 ID，并为每个强调字幕事件给出语义优先级和一个完整 caption_option_id。候选选项已经由本地程序完成帧时间、避脸、避场景文字、版式、字体和动画安全校验。最终数量、时间冲突、最小间隔和 hero 上限全部由本地求解器负责。\n\n口播脚本：\n{script}\n背景音乐候选：\n{bgm_candidates}\n强调字幕窗口与完整选项（event_id 下只能选择该事件给出的 caption_option_id）：\n{caption_windows}\n本地已知冲突与最大可行数量（只供理解，不要自行删事件）：\n{caption_constraints}\n\n选择目标：\n1. bgm_id 只能来自背景音乐候选；没有合适音乐时必须显式输出 null。\n2. caption_windows 中每个 event_id 都输出且只输出一条 caption_choice；priority 取 0-100，表示语义上值得强调的程度，越高越重要。\n3. 为每个事件选择最合适的完整 caption_option_id；event_id 与 caption_option_id 必须原样来自同一个输入事件。\n4. 不要因为事件互相冲突而漏掉其中任何一个。你只做语义排序和样式选择，本地求解器负责最终数量、时间冲突和 hero 上限。\n\n严格职责边界：\n- 只能选择 ID 和填写 priority/reason，不得输出或改写 text、font、font_id、font_size、font_weight、color、outline、start、end、time、frame、rect、x、y、position、coordinates、anchor_id、animation_id、sfx。\n- 不得输出 portrait_plan、broll_plan、media、timeline 或任何任意扩展字段。\n- 三个顶层字段 bgm_id、caption_choices、analysis 必须全部显式存在；字段名 captions 无效。\n- 只输出 JSON，不要 Markdown 或额外说明。\n\n{repair_feedback}\n\n只输出如下 JSON：\n{{\n  \"bgm_id\": \"bgm_xxx\",\n  \"caption_choices\": [{{\"event_id\": \"hz_001\", \"caption_option_id\": \"caption_option_001\", \"priority\": 80, \"reason\": \"为什么选择\"}}],\n  \"analysis\": \"后处理整体选择思路\"\n}}"


def upgrade() -> None:
    """Publish the semantic-ranking-only PostProcess Agent boundary."""

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
                changelog = 'Moved caption legality and fallback into the local solver.',
                approved_at = coalesce(approved_at, now()),
                published_at = coalesce(published_at, now()),
                updated_at = now()
            where id = :version_id
              and prompt_template_id = :template_id
              and content not like :local_solver_marker
            """
        ),
        {
            "content": _current_postprocess_prompt(),
            "version_id": _VERSION_ID,
            "template_id": _TEMPLATE_ID,
            "local_solver_marker": f"%{_LOCAL_SOLVER_MARKER}%",
        },
    )


def downgrade() -> None:
    # Returning count/timing legality to the model would weaken the runtime contract.
    return
