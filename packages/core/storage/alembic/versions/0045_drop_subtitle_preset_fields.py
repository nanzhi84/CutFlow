from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0045_drop_subtitle_preset"
down_revision = "0044_huazi_prompt_boundary"
branch_labels = None
depends_on = None

_REMOVED_KEYS = ("caption_style_pair_id", "emphasis_position_id")


def upgrade() -> None:
    """Strip retired subtitle preset fields from persisted requests/defaults.

    ``SubtitleOptions.caption_style_pair_id`` and ``emphasis_position_id`` were
    removed when subtitle controls became user-configurable instead of preset-driven.
    Historical ``jobs.request`` and ``user_generation_defaults.settings`` rows can
    still carry those nested keys, and strict contract validation rejects them when
    loading runs or saved defaults. Clean both tables so restored legacy rows load
    under the current contract.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("jobs"):
        op.execute(
            """
            update jobs
            set request = jsonb_set(
                request,
                '{subtitle}',
                (request -> 'subtitle')
                    - 'caption_style_pair_id'
                    - 'emphasis_position_id'
            )
            where type = 'digital_human_video'
              and jsonb_typeof(request) = 'object'
              and jsonb_typeof(request -> 'subtitle') = 'object'
              and jsonb_exists_any(
                  request -> 'subtitle',
                  array['caption_style_pair_id', 'emphasis_position_id']
              )
            """
        )
    if inspector.has_table("user_generation_defaults"):
        op.execute(
            """
            update user_generation_defaults
            set settings = jsonb_set(
                settings,
                '{subtitle}',
                (settings -> 'subtitle')
                    - 'caption_style_pair_id'
                    - 'emphasis_position_id'
            )
            where jsonb_typeof(settings) = 'object'
              and jsonb_typeof(settings -> 'subtitle') = 'object'
              and jsonb_exists_any(
                  settings -> 'subtitle',
                  array['caption_style_pair_id', 'emphasis_position_id']
              )
            """
        )


def downgrade() -> None:
    """Best-effort restore of the retired preset fields for code rollback."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("jobs"):
        op.execute(
            """
            update jobs
            set request = jsonb_set(
                request,
                '{subtitle}',
                '{"caption_style_pair_id": "douyin_bold_a", "emphasis_position_id": "top_center_banner"}'::jsonb
                    || (request -> 'subtitle')
            )
            where type = 'digital_human_video'
              and jsonb_typeof(request) = 'object'
              and jsonb_typeof(request -> 'subtitle') = 'object'
            """
        )
    if inspector.has_table("user_generation_defaults"):
        op.execute(
            """
            update user_generation_defaults
            set settings = jsonb_set(
                settings,
                '{subtitle}',
                '{"caption_style_pair_id": "douyin_bold_a", "emphasis_position_id": "top_center_banner"}'::jsonb
                    || (settings -> 'subtitle')
            )
            where jsonb_typeof(settings) = 'object'
              and jsonb_typeof(settings -> 'subtitle') = 'object'
            """
        )
