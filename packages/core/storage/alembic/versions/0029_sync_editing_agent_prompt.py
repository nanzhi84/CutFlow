from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0029_sync_editing_agent_prompt"
down_revision = "0028_clean_user_defaults"
branch_labels = None
depends_on = None

_VERSION_ID = "prompt_editing_agent_v1"
_TEMPLATE_ID = "prompt_editing_agent"


def _current_editing_agent_prompt() -> str:
    path = Path(__file__).resolve().parents[2] / "prompt_group_defaults.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("items", []):
        if item.get("version_id") == _VERSION_ID:
            return str(item["content"])
    raise RuntimeError(f"Missing {_VERSION_ID} in prompt_group_defaults.json")


def upgrade() -> None:
    """Repair legacy DBs whose built-in EditingAgentPlanning prompt predates #136.

    The old published ``prompt_editing_agent_v1`` expected variables such as
    ``asr_segments`` / ``portrait_draft_plan`` and emitted ``broll_overrides``.
    The current node renders the #136 ID-selection contract. Existing databases
    keep the old row because seed insertion is idempotent, so upgrade the built-in
    version in place when those legacy markers are present.
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
              )
            """
        ),
        {"content": content, "version_id": _VERSION_ID, "template_id": _TEMPLATE_ID},
    )


def downgrade() -> None:
    # No safe downgrade: the legacy prompt was incompatible with the current
    # EditingAgentPlanning node and is not reconstructed.
    return
