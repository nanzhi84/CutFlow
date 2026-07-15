from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0055_tts_async_icl2"
down_revision = "0054_tts_single_mp3"
branch_labels = None
depends_on = None

_PROFILE_ID = "volcengine.tts.prod"
_MODEL_ID = "seed-icl-2.0"
_PROVIDER_ID = "volcengine.tts"
_CAPABILITY_ID = "tts.speech"


def upgrade() -> None:
    """Activate async ICL 2.0 only after the application token is explicitly armed.

    Secret material lives outside PostgreSQL, so Alembic cannot inspect whether the
    profile's secret has already been rotated from legacy ``AK:SK`` to the composite
    JSON credential. Operators must rotate it first and set
    ``default_options.async_icl2_ready=true``; unarmed legacy deployments remain on
    their working v1 path instead of being broken by this migration.
    """

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not sa.inspect(bind).has_table("provider_profiles"):
        return
    bind.execute(
        sa.text(
            """
            update provider_profiles
            set model_id = :model_id,
                default_options = (
                    coalesce(default_options, '{}'::jsonb)
                    - 'v3_create_model'
                    - 'v3_model'
                ) || jsonb_build_object(
                    'api_version', 'v3',
                    'resource_id', :model_id,
                    'format', 'mp3',
                    'sample_rate', 24000,
                    'poll_interval', 1.0,
                    'poll_max_attempts', 600
                ),
                updated_at = now()
            where id = :profile_id
              and default_options ->> 'async_icl2_ready' = 'true'
              and (
                  model_id is distinct from :model_id
                  or default_options ->> 'api_version' is distinct from 'v3'
                  or default_options ->> 'resource_id' is distinct from :model_id
                  or default_options ->> 'format' is distinct from 'mp3'
                  or default_options ->> 'sample_rate' is distinct from '24000'
                  or default_options ->> 'poll_interval' is distinct from '1.0'
                  or default_options ->> 'poll_max_attempts' is distinct from '600'
                  or default_options ? 'v3_create_model'
                  or default_options ? 'v3_model'
              )
            """
        ),
        {"profile_id": _PROFILE_ID, "model_id": _MODEL_ID},
    )
    if sa.inspect(bind).has_table("provider_capabilities"):
        bind.execute(
            sa.text(
                """
                update provider_capabilities
                set supports_async_job = true,
                    model_id = case
                        when exists (
                            select 1
                            from provider_profiles
                            where id = :profile_id
                              and model_id = :model_id
                              and default_options ->> 'api_version' = 'v3'
                        ) then :model_id
                        else model_id
                    end,
                    updated_at = now()
                where provider_id = :provider_id
                  and capability_id = :capability_id
                  and (
                      supports_async_job is false
                      or (
                          exists (
                              select 1
                              from provider_profiles
                              where id = :profile_id
                                and model_id = :model_id
                                and default_options ->> 'api_version' = 'v3'
                          )
                          and model_id is distinct from :model_id
                      )
                  )
                """
            ),
            {
                "profile_id": _PROFILE_ID,
                "model_id": _MODEL_ID,
                "provider_id": _PROVIDER_ID,
                "capability_id": _CAPABILITY_ID,
            },
        )


def downgrade() -> None:
    # Re-enabling either fragmented streaming or the legacy v1 model would undo the
    # full-file ICL 2.0 production path, so this data migration is intentionally sticky.
    return
