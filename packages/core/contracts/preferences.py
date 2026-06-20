"""User generation defaults: a per-user saved set of digital-human-video options.

A user can save "my defaults" once and reuse them for one-click / batch generation.
Every field is Optional: a missing block means "use the system default" for that
block. This contract deliberately excludes per-job inputs (``case_id`` / ``script`` /
``title``) so it stays a pure options preset, not a job request.
"""

from __future__ import annotations

from .base import ContractModel
from .jobs import (
    BgmOptions,
    BrollOptions,
    CoverOptions,
    LipSyncOptions,
    OutputOptions,
    PortraitOptions,
    StrictnessOptions,
    SubtitleOptions,
    VoiceOptions,
)


class UserGenerationDefaults(ContractModel):
    """A user's saved generation option preset (all blocks Optional).

    When a block is ``None`` the caller should fall back to the system default for
    that block (i.e. the ``DigitalHumanVideoRequest`` field's default_factory).
    """

    voice: VoiceOptions | None = None
    portrait: PortraitOptions | None = None
    broll: BrollOptions | None = None
    lipsync: LipSyncOptions | None = None
    subtitle: SubtitleOptions | None = None
    bgm: BgmOptions | None = None
    cover: CoverOptions | None = None
    output: OutputOptions | None = None
    strictness: StrictnessOptions | None = None
