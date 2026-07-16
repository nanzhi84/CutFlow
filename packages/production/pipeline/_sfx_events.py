"""Optional frame-synchronous SFX derived from registered caption effects."""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass

from packages.core.contracts.artifacts import CaptionCompositionPlanArtifact
from packages.production.pipeline._caption_effects import caption_effect
from packages.production.pipeline._emphasis_styles import emphasis_style

_COOLDOWN_MS = 180


@dataclass(frozen=True)
class CaptionSfxEvent:
    asset_id: str | None
    sfx_class: str
    start_ms: int
    volume: float
    source_run_id: str


def plan_caption_sfx_events(
    *,
    caption_composition: CaptionCompositionPlanArtifact,
    sfx_asset_ids_by_class: dict[str, str],
) -> list[CaptionSfxEvent]:
    fps = caption_composition.fps
    candidates: list[CaptionSfxEvent] = []
    for cue in caption_composition.cues:
        for line in cue.lines:
            line_level_classes: set[str] = set()
            for run in line.runs:
                effect = caption_effect(run.effect_id)
                style = emphasis_style(run.style_id) if run.style_id else None
                sfx_class = style.sfx_class if style is not None else effect.sfx_class
                if sfx_class is None:
                    continue
                if effect.needs_char_timing:
                    if sfx_class in line_level_classes:
                        continue
                    line_level_classes.add(sfx_class)
                candidates.append(
                    CaptionSfxEvent(
                        asset_id=sfx_asset_ids_by_class.get(sfx_class),
                        sfx_class=sfx_class,
                        start_ms=round(run.enter_frame * 1000 / fps),
                        volume=style.sfx_volume if style is not None else 0.48,
                        source_run_id=run.run_id,
                    )
                )
    missing_by_class: dict[str, CaptionSfxEvent] = {}
    assigned: list[CaptionSfxEvent] = []
    for candidate in candidates:
        if candidate.asset_id is None:
            missing_by_class.setdefault(candidate.sfx_class, candidate)
        else:
            assigned.append(candidate)
    missing = sorted(
        missing_by_class.values(),
        key=lambda item: (item.start_ms, item.sfx_class, item.source_run_id),
    )
    assigned.sort(key=lambda item: (item.start_ms, item.source_run_id))
    return [*missing, *assigned]


def cooldown_caption_sfx_events(
    events: list[CaptionSfxEvent],
    *,
    duration: float,
    playable_asset_ids: Collection[str],
) -> list[CaptionSfxEvent]:
    """Apply cooldown only after callers have proved the asset is readable."""

    kept: list[CaptionSfxEvent] = []
    last_ms = -10_000
    for event in sorted(events, key=lambda item: (item.start_ms, item.source_run_id)):
        if event.asset_id is None or event.asset_id not in playable_asset_ids:
            continue
        if event.start_ms - last_ms < _COOLDOWN_MS:
            continue
        kept.append(event)
        last_ms = event.start_ms
    maximum = max(4, min(24, int(math.ceil(max(0.0, duration) * 0.8))))
    return kept[:maximum]
