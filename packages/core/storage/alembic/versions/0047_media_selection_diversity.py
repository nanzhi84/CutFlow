from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0047_media_selection_diversity"
down_revision = "0046_huazi_subagent_prompt"
branch_labels = None
depends_on = None

_VERSION_ID = "prompt_media_selection_agent_v1"
_TEMPLATE_ID = "prompt_media_selection_agent"
_DIVERSITY_MARKER = "candidate_id | asset_id | diversity_key | scene_name"
_RULE_MARKER = "{broll_uniqueness_rule}"


def _current_media_selection_prompt() -> str:
    path = Path(__file__).resolve().parents[2] / "prompt_group_defaults.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("items", []):
        if item.get("version_id") == _VERSION_ID:
            return str(item["content"])
    raise RuntimeError(f"Missing {_VERSION_ID} in prompt_group_defaults.json")


def upgrade() -> None:
    """Expose and document B-roll diversity constraints in existing databases."""

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
                changelog = 'Exposed B-roll diversity keys and uniqueness rules.',
                approved_at = coalesce(approved_at, now()),
                published_at = coalesce(published_at, now()),
                updated_at = now()
            where id = :version_id
              and prompt_template_id = :template_id
              and (
                content not like :diversity_marker
                or content not like :rule_marker
              )
            """
        ),
        {
            "content": _current_media_selection_prompt(),
            "version_id": _VERSION_ID,
            "template_id": _TEMPLATE_ID,
            "diversity_marker": f"%{_DIVERSITY_MARKER}%",
            "rule_marker": f"%{_RULE_MARKER}%",
        },
    )


def downgrade() -> None:
    # Removing the constraint from the prompt would recreate the production bug.
    return
