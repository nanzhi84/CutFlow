from __future__ import annotations

import json
from pathlib import Path

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
    path = Path(__file__).resolve().parents[2] / "prompt_group_defaults.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("items", []):
        if item.get("version_id") == _VERSION_ID:
            return str(item["content"])
    raise RuntimeError(f"Missing {_VERSION_ID} in prompt_group_defaults.json")


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
