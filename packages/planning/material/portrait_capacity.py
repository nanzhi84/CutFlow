"""Portrait slot capacity helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def can_cover_slots_with_cap(
    required_frames: Sequence[int],
    asset_capacities: Mapping[str, int],
    cap: int,
) -> bool:
    if not required_frames:
        return True
    if cap <= 0 or not asset_capacities:
        return False
    usage = {asset_id: 0 for asset_id in asset_capacities}
    for need in sorted((max(0, int(frames)) for frames in required_frames), reverse=True):
        eligible = [
            asset_id
            for asset_id, capacity in asset_capacities.items()
            if int(capacity) >= need and usage[asset_id] < cap
        ]
        if not eligible:
            return False
        chosen = min(
            eligible,
            key=lambda asset_id: (int(asset_capacities[asset_id]), usage[asset_id], asset_id),
        )
        usage[chosen] += 1
    return True
