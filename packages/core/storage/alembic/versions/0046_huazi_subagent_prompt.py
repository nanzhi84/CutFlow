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
