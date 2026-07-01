from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# Revision id kept <= 32 chars (alembic version_num column limit).
revision = "0028_clean_user_defaults"
down_revision = "0027_drop_portrait_options"
branch_labels = None
depends_on = None

_TABLE = "user_generation_defaults"


def upgrade() -> None:
    """Strip retired option keys from saved per-user generation defaults.

    Earlier cleanup migrations removed contract-deleted keys from historical
    ``jobs.request`` rows, and 0027 also removed the retired top-level
    ``portrait`` block from ``user_generation_defaults.settings``. Saved defaults
    can still carry the older nested LipSync / Output / Strictness keys, though,
    and ``UserGenerationDefaults.model_validate`` rejects them under
    ``extra="forbid"``. Clean the same retired nested keys here so restored legacy
    databases can load the user's saved preset.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    op.execute(
        f"""
        update {_TABLE}
        set settings = jsonb_set(
            settings,
            '{{lipsync}}',
            (settings -> 'lipsync')
                - 'ref_image_artifact_id'
                - 'video_extension'
                - 'query_face_threshold'
        )
        where jsonb_typeof(settings) = 'object'
          and jsonb_typeof(settings -> 'lipsync') = 'object'
          and jsonb_exists_any(
              settings -> 'lipsync',
              array['ref_image_artifact_id', 'video_extension', 'query_face_threshold']
          )
        """
    )
    op.execute(
        f"""
        update {_TABLE}
        set settings = jsonb_set(
            settings,
            '{{output}}',
            (settings -> 'output')
                - 'export_jianying_draft'
                - 'export_editor_handoff'
                - 'upload_to_oss'
                - 'keep_local_originals'
                - 'format'
        )
        where jsonb_typeof(settings) = 'object'
          and jsonb_typeof(settings -> 'output') = 'object'
          and jsonb_exists_any(
              settings -> 'output',
              array[
                  'export_jianying_draft',
                  'export_editor_handoff',
                  'upload_to_oss',
                  'keep_local_originals',
                  'format'
              ]
          )
        """
    )
    op.execute(
        f"""
        update {_TABLE}
        set settings = jsonb_set(
            settings,
            '{{strictness}}',
            (settings -> 'strictness')
                - 'broll_insufficient_policy'
                - 'bgm_unavailable_policy'
                - 'strict_cost_pricing'
        )
        where jsonb_typeof(settings) = 'object'
          and jsonb_typeof(settings -> 'strictness') = 'object'
          and jsonb_exists_any(
              settings -> 'strictness',
              array[
                  'broll_insufficient_policy',
                  'bgm_unavailable_policy',
                  'strict_cost_pricing'
              ]
          )
        """
    )


def downgrade() -> None:
    """Best-effort restore of the retired saved-default keys."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    op.execute(
        f"""
        update {_TABLE}
        set settings = jsonb_set(
            settings,
            '{{lipsync}}',
            '{{"ref_image_artifact_id": null, "video_extension": false, "query_face_threshold": null}}'::jsonb
                || (settings -> 'lipsync')
        )
        where jsonb_typeof(settings) = 'object'
          and jsonb_typeof(settings -> 'lipsync') = 'object'
        """
    )
    op.execute(
        f"""
        update {_TABLE}
        set settings = jsonb_set(
            settings,
            '{{output}}',
            '{{"export_jianying_draft": true, "export_editor_handoff": true, "upload_to_oss": true, "keep_local_originals": false, "format": "mp4"}}'::jsonb
                || (settings -> 'output')
        )
        where jsonb_typeof(settings) = 'object'
          and jsonb_typeof(settings -> 'output') = 'object'
        """
    )
    op.execute(
        f"""
        update {_TABLE}
        set settings = jsonb_set(
            settings,
            '{{strictness}}',
            '{{"broll_insufficient_policy": "soft_degrade", "bgm_unavailable_policy": "soft_degrade", "strict_cost_pricing": false}}'::jsonb
                || (settings -> 'strictness')
        )
        where jsonb_typeof(settings) = 'object'
          and jsonb_typeof(settings -> 'strictness') = 'object'
        """
    )
