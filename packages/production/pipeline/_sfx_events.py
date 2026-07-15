"""Deterministic huazi-to-SFX event planning and density control.

Normal captions are intentionally absent from this module: their visual entrance
effects never imply audio.  Only an explicit ``sfx_id`` selected for a huazi event
may produce a mix event.
"""

from __future__ import annotations

import math

_SAME_ASSET_COOLDOWN_MS = 180


def plan_huazi_sfx_events(
    *,
    overlay_events: list[dict],
    duration: float,
) -> list[dict]:
    candidates: list[dict] = []
    for index, event in enumerate(overlay_events):
        asset_id = str(event.get("sfx_id") or "none")
        if asset_id == "none":
            continue
        candidates.append(
            {
                "asset_id": asset_id,
                "start_ms": round(float(event.get("start") or 0.0) * 1000),
                "priority": 50 + int(event.get("priority") or 0),
                "volume": 0.62 if event.get("visual_preset_id") == "hero" else 0.48,
                "source_event_id": str(event.get("event_id") or f"overlay_{index:03d}"),
            }
        )

    # Same-frame de-duplication: retain the semantically stronger event.
    by_frame: dict[int, dict] = {}
    for candidate in sorted(
        candidates,
        key=lambda item: (item["start_ms"], -item["priority"], item["asset_id"]),
    ):
        frame_bucket = round(candidate["start_ms"] / 33.333)
        current = by_frame.get(frame_bucket)
        if current is None or candidate["priority"] > current["priority"]:
            by_frame[frame_bucket] = candidate

    kept: list[dict] = []
    last_by_asset: dict[str, int] = {}
    last_event_ms = -10_000
    for candidate in sorted(by_frame.values(), key=lambda item: (item["start_ms"], -item["priority"])):
        asset_id = candidate["asset_id"]
        if candidate["start_ms"] - last_event_ms < _SAME_ASSET_COOLDOWN_MS:
            continue
        if (
            candidate["start_ms"] - last_by_asset.get(asset_id, -10_000)
            < _SAME_ASSET_COOLDOWN_MS
        ):
            continue
        last_by_asset[asset_id] = candidate["start_ms"]
        last_event_ms = candidate["start_ms"]
        kept.append(candidate)

    max_events = max(4, min(24, int(math.ceil(max(0.0, duration) * 0.8))))
    if len(kept) <= max_events:
        return kept
    selected = sorted(kept, key=lambda item: (-item["priority"], item["start_ms"]))[:max_events]
    return sorted(selected, key=lambda item: (item["start_ms"], -item["priority"]))
