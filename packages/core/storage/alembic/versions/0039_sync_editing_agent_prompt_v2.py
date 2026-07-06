from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0039_sync_edit_prompt_v2"
down_revision = "0038_clip_embedding_jobs"
branch_labels = None
depends_on = None

_VERSION_ID = "prompt_editing_agent_v1"
_TEMPLATE_ID = "prompt_editing_agent"
_PORTRAIT_LINE_MARKER = "candidate_id | asset_id | available_seconds | description | reason"
_BROLL_LINE_MARKER = (
    "candidate_id | asset_id | scene_name | allowed_slot_ids | matched_keywords | "
    "available_seconds | description"
)


def _current_editing_agent_prompt() -> str:
    path = Path(__file__).resolve().parents[2] / "prompt_group_defaults.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("items", []):
        if item.get("version_id") == _VERSION_ID:
            return str(item["content"])
    raise RuntimeError(f"Missing {_VERSION_ID} in prompt_group_defaults.json")


def upgrade() -> None:
    """Sync built-in EditingAgentPlanning prompt to the compact line candidate format."""
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
                changelog = 'Synced built-in EditingAgentPlanning prompt line-format contract.',
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
                or content not like :portrait_line_marker
                or content not like :broll_line_marker
                or content like '%允许重复使用同一素材%'
                or content not like '%{portrait_uniqueness_rule}%'
              )
            """
        ),
        {
            "content": content,
            "version_id": _VERSION_ID,
            "template_id": _TEMPLATE_ID,
            "portrait_line_marker": f"%{_PORTRAIT_LINE_MARKER}%",
            "broll_line_marker": f"%{_BROLL_LINE_MARKER}%",
        },
    )


def downgrade() -> None:
    # No safe downgrade: older prompts exposed frame bookkeeping to the LLM.
    return
