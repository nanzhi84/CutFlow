"""Optional frame-synchronous SFX derived from emphasis CaptionRuns."""

from __future__ import annotations

import math
from dataclasses import dataclass

from packages.core.contracts.artifacts import CaptionCompositionPlanArtifact

_COOLDOWN_MS = 180


@dataclass(frozen=True)
class EmphasisSfxEvent:
    asset_id: str
    start_ms: int
    priority: int
    volume: float
    source_run_id: str


def plan_emphasis_sfx_events(
    *,
    caption_composition: CaptionCompositionPlanArtifact,
    duration: float,
    sfx_asset_id: str | None,
) -> list[EmphasisSfxEvent]:
    if not sfx_asset_id:
        return []
    fps = caption_composition.fps
    candidates: list[EmphasisSfxEvent] = []
    for cue in caption_composition.cues:
        for line in cue.lines:
            for run in line.runs:
                if run.role != "emphasis" or run.effect_id != "pop":
                    continue
                candidates.append(
                    EmphasisSfxEvent(
                        asset_id=sfx_asset_id,
                        start_ms=round(run.enter_frame * 1000 / fps),
                        priority=50,
                        volume=0.48,
                        source_run_id=run.run_id,
                    )
                )
    kept: list[EmphasisSfxEvent] = []
    last_ms = -10_000
    for candidate in sorted(candidates, key=lambda item: (item.start_ms, item.source_run_id)):
        if candidate.start_ms - last_ms < _COOLDOWN_MS:
            continue
        kept.append(candidate)
        last_ms = candidate.start_ms
    maximum = max(4, min(24, int(math.ceil(max(0.0, duration) * 0.8))))
    return kept[:maximum]
