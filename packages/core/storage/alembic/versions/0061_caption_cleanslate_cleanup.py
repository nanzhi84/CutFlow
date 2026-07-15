from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0061_caption_cleanslate_cleanup"
down_revision = "0060_creative_intent_runs"
branch_labels = None
depends_on = None

_OLD_ARTIFACT_KINDS = (
    "plan.caption_windows",
    "plan.caption_display",
    "plan.postprocess_diagnostics",
)
_OLD_CODES = (
    "huazi.animation_fallback",
    "huazi.planning_failed",
    "caption.visual_analysis_failed",
    "caption.normal_relaxed_safety",
    "caption.emphasis_relaxed_safety",
    "caption.emphasis_below_floor",
    "postprocess.planning_failed",
)


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
                where prompt_template_id in ('prompt_huazi_subagent', 'prompt_postprocess_agent')
                   or node_id in ('HuaziPlanningSubagent', 'PostProcessAgentPlanning')
                """
            )
        )
    if inspector.has_table("prompt_versions"):
        bind.execute(
            sa.text(
                """
                delete from prompt_versions
                where prompt_template_id in ('prompt_huazi_subagent', 'prompt_postprocess_agent')
                """
            )
        )
    if inspector.has_table("prompt_templates"):
        bind.execute(
            sa.text(
                """
                delete from prompt_templates
                where id in ('prompt_huazi_subagent', 'prompt_postprocess_agent')
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
                            where kind = any(cast(:old_artifact_kinds as varchar[]))
                        )
                    ),
                    '{}'::varchar[]
                )
                where output_artifact_ids && array(
                    select id from artifacts
                    where kind = any(cast(:old_artifact_kinds as varchar[]))
                )
                """
            ),
            {"old_artifact_kinds": list(_OLD_ARTIFACT_KINDS)},
        )
        bind.execute(
            sa.text(
                "delete from artifacts "
                "where kind = any(cast(:old_artifact_kinds as varchar[]))"
            ),
            {"old_artifact_kinds": list(_OLD_ARTIFACT_KINDS)},
        )
        bind.execute(
            sa.text(
                """
                update artifacts
                set payload = payload - 'overlay_events'
                where kind = 'plan.style'
                  and jsonb_typeof(payload) = 'object'
                  and payload ? 'overlay_events'
                """
            )
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
            {"old_codes": list(_OLD_CODES)},
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
            {"old_codes": list(_OLD_CODES)},
        )

    _normalize_subtitle_settings(bind, inspector, "jobs", "request", "type = 'digital_human_video'")
    _normalize_subtitle_settings(bind, inspector, "user_generation_defaults", "settings", "true")


def _normalize_subtitle_settings(bind, inspector, table: str, column: str, extra_where: str) -> None:
    if not inspector.has_table(table):
        return
    bind.execute(
        sa.text(
            f"""
            update {table}
            set {column} = jsonb_set(
                {column},
                '{{subtitle}}',
                case
                    when coalesce(({column} -> 'subtitle' ->> 'enabled')::boolean, true)
                     and coalesce(
                         ({column} -> 'subtitle' ->> 'normal_enabled')::boolean,
                         coalesce(({column} -> 'subtitle' ->> 'enabled')::boolean, true)
                     )
                    then ({column} -> 'subtitle')
                    else jsonb_set(
                        jsonb_set(
                            {column} -> 'subtitle',
                            '{{normal_enabled}}',
                            'false'::jsonb,
                            true
                        ),
                        '{{emphasis_enabled}}',
                        'false'::jsonb,
                        true
                    )
                end
                    - 'caption_style_pair_id'
                    - 'emphasis_position_id'
                    - 'emphasis_animation_id',
                true
            )
            where {extra_where}
              and jsonb_typeof({column}) = 'object'
              and jsonb_typeof({column} -> 'subtitle') = 'object'
            """
        )
    )


def downgrade() -> None:
    # Historical caption payloads and retired prompt rows cannot be reconstructed safely.
    return
