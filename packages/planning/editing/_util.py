"""Small pure helpers shared across the editing-agent boundary planner.

Behaviour copied from editing_agent/util.py. ``round_time`` keeps the 3-decimal
rounding the origin uses for boundary bookkeeping; the FINAL frame quantization is
done by :mod:`packages.planning.editing.frame_grid` (the single source of truth).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from packages.core.contracts.artifacts import NarrationUnit


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def round_time(value: float) -> float:
    return round(max(0.0, float(value)), 3)


def overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def map_unit_ids_to_range(
    narration_units: Sequence[NarrationUnit],
    start: float,
    end: float,
) -> list[str]:
    mapped: list[str] = []
    for unit in narration_units:
        if overlap(start, end, unit.start, unit.end) > 0:
            mapped.append(unit.unit_id)
    return mapped


def unit_duration(unit: NarrationUnit) -> float:
    if unit.duration is not None:
        return float(unit.duration)
    return max(0.0, float(unit.end) - float(unit.start))
