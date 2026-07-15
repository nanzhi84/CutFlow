"""Optional frame-synchronous SFX derived from emphasis CaptionRuns."""

from __future__ import annotations

import math

_COOLDOWN_MS = 180


def plan_emphasis_sfx_events(
    *,
    caption_composition: dict,
    duration: float,
    sfx_asset_id: str | None,
) -> list[dict]:
    if not sfx_asset_id:
        return []
    fps = max(1, int(caption_composition.get("fps") or 30))
    candidates: list[dict] = []
    for cue in caption_composition.get("cues") or []:
        for line in cue.get("lines") or []:
            for run in line.get("runs") or []:
                if run.get("role") != "emphasis" or run.get("effect_id") != "pop":
                    continue
                candidates.append(
                    {
                        "asset_id": sfx_asset_id,
                        "start_ms": round(int(run.get("enter_frame") or 0) * 1000 / fps),
                        "priority": 50,
                        "volume": 0.48,
                        "source_run_id": str(run.get("run_id") or ""),
                    }
                )
    kept: list[dict] = []
    last_ms = -10_000
    for candidate in sorted(candidates, key=lambda item: (item["start_ms"], item["source_run_id"])):
        if candidate["start_ms"] - last_ms < _COOLDOWN_MS:
            continue
        kept.append(candidate)
        last_ms = candidate["start_ms"]
    maximum = max(4, min(24, int(math.ceil(max(0.0, duration) * 0.8))))
    return kept[:maximum]
