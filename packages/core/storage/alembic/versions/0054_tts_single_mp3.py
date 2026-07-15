from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0054_tts_single_mp3"
down_revision = "0053_postprocess_local_solver"
branch_labels = None
depends_on = None

_PROFILE_ID = "volcengine.tts.prod"


def upgrade() -> None:
    """Use one synchronous full-script MP3 for production Volcengine TTS."""

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not sa.inspect(bind).has_table("provider_profiles"):
        return
    bind.execute(
        sa.text(
            """
            update provider_profiles
            set default_options = jsonb_set(
                    coalesce(default_options, '{}'::jsonb),
                    '{api_version}',
                    '"v1"'::jsonb,
                    true
                ),
                updated_at = now()
            where id = :profile_id
              and default_options ->> 'api_version' is distinct from 'v1'
            """
        ),
        {"profile_id": _PROFILE_ID},
    )


def downgrade() -> None:
    # Re-enabling the fragmented v3 audio path would reintroduce audible pauses.
    return
