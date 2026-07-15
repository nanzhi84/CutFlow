from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0056_media_prompt_domains"
down_revision = "0055_tts_async_icl2"
branch_labels = None
depends_on = None

_TEMPLATE_ID = "prompt_media_selection_agent"
_V1_VERSION_ID = "prompt_media_selection_agent_v1"
_V2_VERSION_ID = "prompt_media_selection_agent_v2"
_BINDING_ID = "prompt_binding_prompt_media_selection_agent"
_NODE_ID = "MediaSelectionAgentPlanning"
_CHANGELOG = "Publish slot-scoped, construction-safe media candidate domains."


def _v2_seed_item() -> dict:
    path = Path(__file__).resolve().parents[2] / "prompt_group_defaults.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("items", []):
        if item.get("version_id") == _V2_VERSION_ID:
            return item
    raise RuntimeError(f"Missing {_V2_VERSION_ID} in prompt_group_defaults.json")


def upgrade() -> None:
    """Publish v2 and atomically move the node binding without rewriting v1."""

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("prompt_templates") or not inspector.has_table("prompt_versions"):
        return

    item = _v2_seed_item()
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
            "id": _TEMPLATE_ID,
            "name": str(item["name"]),
            "purpose": str(item["purpose"]),
            "variables_schema_ref": json.dumps({"schema_id": item["variables_schema_id"]}),
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
            on conflict (id) do update
            set content = excluded.content,
                status = 'published',
                changelog = excluded.changelog,
                approved_at = coalesce(prompt_versions.approved_at, now()),
                published_at = coalesce(prompt_versions.published_at, now()),
                updated_at = now()
            where prompt_versions.prompt_template_id = excluded.prompt_template_id
            """
        ),
        {
            "id": _V2_VERSION_ID,
            "template_id": _TEMPLATE_ID,
            "content": str(item["content"]),
            "changelog": _CHANGELOG,
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
                on conflict (id) do update
                set prompt_template_id = excluded.prompt_template_id,
                    prompt_version_id = excluded.prompt_version_id,
                    node_id = excluded.node_id,
                    enabled = true,
                    updated_at = now()
                """
            ),
            {
                "id": _BINDING_ID,
                "template_id": _TEMPLATE_ID,
                "version_id": _V2_VERSION_ID,
                "node_id": _NODE_ID,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not sa.inspect(bind).has_table("prompt_bindings"):
        return
    bind.execute(
        sa.text(
            """
            update prompt_bindings
            set prompt_version_id = :version_id,
                updated_at = now()
            where id = :binding_id
              and prompt_template_id = :template_id
              and prompt_version_id = :v2_version_id
            """
        ),
        {
            "binding_id": _BINDING_ID,
            "template_id": _TEMPLATE_ID,
            "version_id": _V1_VERSION_ID,
            "v2_version_id": _V2_VERSION_ID,
        },
    )
