from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0062_drop_v1_prompts"
down_revision = "0061_caption_cleanslate_cleanup"
branch_labels = None
depends_on = None

_OLD_V1_CODES = (
    "editing_agent.deterministic_fallback",
    "editing_agent.llm_repair",
    "editing_agent.local_constraint_repair",
)
_OLD_V1_ARTIFACT_KINDS = ("plan.editing_diagnostics",)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("prompt_bindings"):
        bind.execute(
            sa.text(
                """
                delete from prompt_bindings
                where prompt_template_id = 'prompt_editing_agent'
                   or node_id = 'EditingAgentPlanning'
                """
            )
        )
    if inspector.has_table("prompt_versions"):
        bind.execute(
            sa.text(
                """
                delete from prompt_versions version
                where version.prompt_template_id = 'prompt_editing_agent'
                  and not exists (
                      select 1
                      from prompt_invocations invocation
                      where invocation.prompt_version_id = version.id
                  )
                """
            )
        )
    if inspector.has_table("prompt_templates"):
        bind.execute(
            sa.text(
                """
                delete from prompt_templates template
                where template.id = 'prompt_editing_agent'
                  and not exists (
                      select 1
                      from prompt_versions version
                      where version.prompt_template_id = template.id
                  )
                  and not exists (
                      select 1
                      from prompt_invocations invocation
                      where invocation.prompt_template_id = template.id
                  )
                  and not exists (
                      select 1
                      from prompt_experiments experiment
                      where experiment.prompt_template_id = template.id
                  )
                """
            )
        )
    if inspector.has_table("artifacts") and inspector.has_table("node_runs"):
        bind.execute(
            sa.text(
                """
                update node_runs
                set output_artifact_ids = coalesce(
                    (
                        select array_agg(artifact_id order by ordinal)
                        from unnest(output_artifact_ids) with ordinality as ids(artifact_id, ordinal)
                        where artifact_id not in (
                            select id from artifacts
                            where kind = any(cast(:old_kinds as varchar[]))
                        )
                    ),
                    '{}'::varchar[]
                )
                where output_artifact_ids && array(
                    select id from artifacts
                    where kind = any(cast(:old_kinds as varchar[]))
                )
                """
            ),
            {"old_kinds": list(_OLD_V1_ARTIFACT_KINDS)},
        )
        bind.execute(
            sa.text("delete from artifacts where kind = any(cast(:old_kinds as varchar[]))"),
            {"old_kinds": list(_OLD_V1_ARTIFACT_KINDS)},
        )
    if inspector.has_table("node_runs"):
        bind.execute(
            sa.text(
                """
                update node_runs
                set warnings = coalesce(
                        (
                            select array_agg(code)
                            from unnest(warnings) code
                            where code <> all(cast(:old_codes as varchar[]))
                        ),
                        '{}'::varchar[]
                    ),
                    degradations = coalesce(
                        (
                            select jsonb_agg(item)
                            from jsonb_array_elements(degradations) item
                            where coalesce(item ->> 'code', trim(both '"' from item::text))
                                  <> all(cast(:old_codes as varchar[]))
                        ),
                        '[]'::jsonb
                    )
                where warnings && cast(:old_codes as varchar[])
                   or exists (
                        select 1
                        from jsonb_array_elements(degradations) item
                        where coalesce(item ->> 'code', trim(both '"' from item::text))
                              = any(cast(:old_codes as varchar[]))
                   )
                """
            ),
            {"old_codes": list(_OLD_V1_CODES)},
        )
    if inspector.has_table("artifacts"):
        bind.execute(
            sa.text(
                """
                update artifacts
                set payload = jsonb_set(
                    jsonb_set(
                        payload,
                        '{warnings}',
                        coalesce(
                            (
                                select jsonb_agg(value)
                                from jsonb_array_elements(
                                    case when jsonb_typeof(payload -> 'warnings') = 'array'
                                         then payload -> 'warnings' else '[]'::jsonb end
                                ) value
                                where trim(both '"' from value::text)
                                      <> all(cast(:old_codes as varchar[]))
                            ),
                            '[]'::jsonb
                        ),
                        true
                    ),
                    '{degradations}',
                    coalesce(
                        (
                            select jsonb_agg(value)
                            from jsonb_array_elements(
                                case when jsonb_typeof(payload -> 'degradations') = 'array'
                                     then payload -> 'degradations' else '[]'::jsonb end
                            ) value
                            where coalesce(value ->> 'code', trim(both '"' from value::text))
                                  <> all(cast(:old_codes as varchar[]))
                        ),
                        '[]'::jsonb
                    ),
                    true
                )
                where kind in ('run.report.public', 'run.report.debug')
                  and jsonb_typeof(payload) = 'object'
                """
            ),
            {"old_codes": list(_OLD_V1_CODES)},
        )


def downgrade() -> None:
    return
