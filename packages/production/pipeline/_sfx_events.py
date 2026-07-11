"""Deterministic caption-to-SFX event planning and density control."""

from __future__ import annotations

import math

_NORMAL_CLICK_ASSET_ID = "asset_sfx_click"
_NORMAL_CLICK_COOLDOWN_MS = 450
_SAME_ASSET_COOLDOWN_MS = 180


def plan_caption_sfx_events(
    *,
    normal_cues: list[dict],
    overlay_events: list[dict],
    duration: float,
) -> list[dict]:
    candidates: list[dict] = []
    for index, cue in enumerate(normal_cues):
        if str(cue.get("effect_id") or "") != "soft_in":
            continue
        candidates.append(
            {
                "asset_id": _NORMAL_CLICK_ASSET_ID,
                "start_ms": round(float(cue.get("start") or 0.0) * 1000),
                "priority": 10,
                "volume": 0.28,
                "source_event_id": str(cue.get("window_id") or f"normal_{index:03d}"),
            }
        )
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
        cooldown = (
            _NORMAL_CLICK_COOLDOWN_MS
            if asset_id == _NORMAL_CLICK_ASSET_ID
            else _SAME_ASSET_COOLDOWN_MS
        )
        if candidate["start_ms"] - last_event_ms < _SAME_ASSET_COOLDOWN_MS:
            continue
        if candidate["start_ms"] - last_by_asset.get(asset_id, -10_000) < cooldown:
            continue
        last_by_asset[asset_id] = candidate["start_ms"]
        last_event_ms = candidate["start_ms"]
        kept.append(candidate)

    max_events = max(4, min(24, int(math.ceil(max(0.0, duration) * 0.8))))
    if len(kept) <= max_events:
        return kept
    selected = sorted(kept, key=lambda item: (-item["priority"], item["start_ms"]))[:max_events]
    return sorted(selected, key=lambda item: (item["start_ms"], -item["priority"]))
