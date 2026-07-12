from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0049_broll_legal_candidates"
down_revision = "0048_emphasis_floor_prompts"
branch_labels = None
depends_on = None

_VERSION_ID = "prompt_media_selection_agent_v1"
_TEMPLATE_ID = "prompt_media_selection_agent"
_LEGAL_CANDIDATES_MARKER = "B-roll 插槽（candidate_id 必须来自对应 legal_candidate_ids"
_LEGACY_ALLOWED_SLOTS_MARKER = "allowed_slot_ids"


def _current_media_selection_prompt() -> str:
    path = Path(__file__).resolve().parents[2] / "prompt_group_defaults.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("items", []):
        if item.get("version_id") == _VERSION_ID:
            return str(item["content"])
    raise RuntimeError(f"Missing {_VERSION_ID} in prompt_group_defaults.json")


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
