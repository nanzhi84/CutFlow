from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0046_huazi_subagent_prompt"
down_revision = "0045_drop_subtitle_preset"
branch_labels = None
depends_on = None

# Caption Display v2 (issue #188): huazi (emphasis caption) planning moves out of
# the main EditingAgentPlanning prompt into a separate HuaziPlanningSubagent (a
# second LLM pass). This migration keeps existing DBs correct on a migrate-only
# deploy path, independent of whether/when ``seed_database`` runs:
#   * the stored ``prompt_editing_agent_v1`` content is re-synced to the
#     huazi-free version (guarded so only pre-#188 rows are touched);
#   * the new ``prompt_huazi_subagent`` template / version / binding are inserted
#     if missing. ``seed_database`` also inserts these on the next bootstrap /
#     startup (it adds any missing seed row), so this insert is a redundant,
#     idempotent safety net that matches the seeded ids exactly.
_EDITING_VERSION_ID = "prompt_editing_agent_v1"
_EDITING_TEMPLATE_ID = "prompt_editing_agent"
_LEGACY_HUAZI_MARKER = "huazi_plan"

_HUAZI_TEMPLATE_ID = "prompt_huazi_subagent"
_HUAZI_VERSION_ID = "prompt_huazi_subagent_v1"
_HUAZI_BINDING_ID = "prompt_binding_prompt_huazi_subagent"
_HUAZI_NODE_ID = "HuaziPlanningSubagent"


def _seed_item(version_id: str) -> dict:
    path = Path(__file__).resolve().parents[2] / "prompt_group_defaults.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("items", []):
        if item.get("version_id") == version_id:
            return item
    raise RuntimeError(f"Missing {version_id} in prompt_group_defaults.json")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("prompt_versions") or not inspector.has_table("prompt_templates"):
        return

    editing_item = _seed_item(_EDITING_VERSION_ID)
    huazi_item = _seed_item(_HUAZI_VERSION_ID)

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

    # 2. Insert the huazi subagent template / version / binding if absent.
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
            "id": _HUAZI_TEMPLATE_ID,
            "name": str(huazi_item["name"]),
            "purpose": str(huazi_item["purpose"]),
            "variables_schema_ref": json.dumps(
                {"schema_id": huazi_item["variables_schema_id"]}
            ),
            "output_schema_ref": json.dumps({"schema_id": huazi_item["output_schema_id"]}),
        },
    )
    bind.execute(
        sa.text(
            """
            insert into prompt_versions
                (id, prompt_template_id, content, status, changelog,
                 approved_at, published_at, schema_version, created_at, updated_at)
            values
                (:id, :template_id, :content, 'published',
                 'Seed HuaziPlanningSubagent prompt (#188).', now(), now(), 'v1', now(), now())
            on conflict (id) do nothing
            """
        ),
        {
            "id": _HUAZI_VERSION_ID,
            "template_id": _HUAZI_TEMPLATE_ID,
            "content": str(huazi_item["content"]),
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
                "id": _HUAZI_BINDING_ID,
                "template_id": _HUAZI_TEMPLATE_ID,
                "version_id": _HUAZI_VERSION_ID,
                "node_id": _HUAZI_NODE_ID,
            },
        )


def downgrade() -> None:
    return
