from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0042_edit_agent_fullcov_prompt"
down_revision = "0041_bgm_mood_prompt_sync"
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
    """Sync EditingAgentPlanning prompt to the full_coverage stitching contract."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("prompt_versions"):
        return
    bind.execute(
        sa.text(
            """
            update prompt_versions
            set content = :content,
                status = 'published',
                changelog = 'Synced EditingAgentPlanning full_coverage stitching prompt.',
                approved_at = coalesce(approved_at, now()),
                published_at = coalesce(published_at, now()),
                updated_at = now()
            where id = :version_id
              and prompt_template_id = :template_id
              and content not like '%multi_clip_allowed%'
            """
        ),
        {
            "content": _current_editing_agent_prompt(),
            "version_id": _VERSION_ID,
            "template_id": _TEMPLATE_ID,
        },
    )

    if inspector.has_table("prompt_bindings"):
        bind.execute(
            sa.text(
                """
                update prompt_bindings
                set prompt_version_id = :version_id,
                    updated_at = now()
                where prompt_template_id = :template_id
                  and node_id = 'EditingAgentPlanning'
                """
            ),
            {
                "version_id": _VERSION_ID,
                "template_id": _TEMPLATE_ID,
            },
        )


def downgrade() -> None:
    return
