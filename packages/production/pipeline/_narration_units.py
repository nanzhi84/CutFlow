"""Shared narration-unit rebuild used by boundary + timeline-window planning.

Given the aligned raw narration units, rebuild the boundary-annotated ``NarrationUnit``
list the editing planner needs (``portrait_cut_allowed`` / ``hard_end`` /
``boundary_score`` / ``boundary_reason`` / ``pause_after_ms``). When the aligned artifact
already carries a boundary signal it is used as-is; otherwise the units are rebuilt from
the script + spoken spans.

This is a PURE deterministic function, so ``NarrationBoundaryPlanning`` (which detects
audio pauses and assembles safe cuts) and ``TimelineWindowPlanning`` (which packs portrait
material into the same boundaries) each derive identical planner units independently —
no node-to-node coupling, and the boundary set is bit-for-bit the same on both sides.
"""

from __future__ import annotations

from pydantic import ValidationError

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.editing import (
    SpokenSegment,
    build_narration_units,
    build_narration_units_from_asr,
)


def build_planner_narration_units(
    *,
    raw_units: list[dict],
    source: str = "",
    script: str,
    duration: float,
) -> list[NarrationUnit]:
    parsed: list[NarrationUnit] = []
    has_boundary_signal = False
    for raw in raw_units or []:
        if not isinstance(raw, dict):
            continue
        try:
            unit = NarrationUnit.model_validate(raw)
        except ValidationError:
            continue
        if unit.end <= unit.start:
            continue
        if unit.duration is None:
            unit = unit.model_copy(update={"duration": round(unit.end - unit.start, 3)})
        parsed.append(unit)
        has_boundary_signal = has_boundary_signal or bool(
            unit.portrait_cut_allowed
            or unit.hard_end
            or unit.pause_after_ms > 0
            or unit.boundary_score > 0
            or str(unit.boundary_reason or "").strip()
        )
    if parsed and has_boundary_signal:
        return parsed

    # Artifacts without boundary fields are rebuilt on resume.
    spoken = [
        SpokenSegment(start=unit.start, end=unit.end, text=unit.text)
        for unit in parsed
        if str(unit.text or "").strip()
    ]
    if parsed and source in {"asr", "tts_subtitle"}:
        units = build_narration_units_from_asr(spoken, duration)
        if units:
            return units
    return build_narration_units(
        script=script,
        asr_segments=spoken or None,
        video_duration=duration,
    )
