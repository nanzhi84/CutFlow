"""NarrationBoundaryPlanning node: front-move pause detection + safe-cut planning (#135).

Placed right after ``NarrationAlignment``. It reads the aligned narration units + the
produced TTS audio, detects real audio pauses (ffmpeg ``silencedetect``), assembles the
semantic + audio-pause safe-cut boundaries, and emits a frame-quantized
``plan.narration_boundary`` artifact.

This is the ONE node that reads ``audio_tts`` for pause detection: TimelineWindowPlanning
and EditingAgentPlanning consume ``pause_windows`` from this artifact instead of
re-running ffmpeg. The boundary assembly is the same pure planning function packing
uses internally, so portrait frame boundaries are unchanged — this node only front-moves
the "where can we safely cut" responsibility, it does not fill any slot.
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind
from packages.core.contracts.artifacts import NarrationBoundaryPlan, NarrationUnit
from packages.core.workflow import NodeOutput
from packages.media.audio import detect_silence_windows
from packages.planning.editing import (
    TIMELINE_FPS,
    build_semantic_audio_boundary_entries,
    frame_index,
)
from packages.production.pipeline._narration_units import build_planner_narration_units
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    narration = state.require(ArtifactKind.narration_units).payload or {}
    raw_units = narration.get("units", []) or []
    duration = max([float(unit.get("end", 0)) for unit in raw_units] or [1.0])

    planner_units = build_planner_narration_units(
        raw_units=raw_units,
        source=str(narration.get("source") or ""),
        script=state.request.script,
        duration=duration,
    )

    # Detect real audio pauses on the produced TTS audio (semantic-only fallback when the
    # audio is the sandbox tone and has no reliable silences). This is the ffmpeg call
    # that used to live downstream from boundary planning.
    pause_windows = _detect_audio_pauses(ctx)

    # The boundary planner also returns a diagnostic trace; this node publishes counts, not
    # the raw trace, so it is intentionally discarded here.
    boundary_entries, _ = build_semantic_audio_boundary_entries(
        planner_units,
        duration,
        pause_windows=pause_windows or None,
    )

    total_frames = max(0, frame_index(duration))
    safe_cut_boundaries = _safe_cut_boundaries(boundary_entries)
    portrait_slots = _portrait_slots(safe_cut_boundaries, planner_units)
    broll_slots = _broll_slots(planner_units, total_frames=total_frames)
    source = "tts_subtitle+silence" if pause_windows else "semantic_only"

    payload = NarrationBoundaryPlan(
        fps=TIMELINE_FPS,
        total_frames=total_frames,
        source=source,
        pause_windows=pause_windows,
        safe_cut_boundaries=safe_cut_boundaries,
        portrait_slots=portrait_slots,
        broll_slots=broll_slots,
        diagnostics={
            "used_audio_pauses": bool(pause_windows),
            "pause_window_count": len(pause_windows),
            "safe_cut_count": len(safe_cut_boundaries),
            "portrait_slot_count": len(portrait_slots),
            "boundary_strategy": source,
        },
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_narration_boundary,
                payload,
                "NarrationBoundaryPlan.v1",
            )
        ]
    )


def _detect_audio_pauses(ctx: NodeContext) -> list[dict]:
    audio = ctx.state.artifacts.get(ArtifactKind.audio_tts)
    if audio is None or not audio.uri:
        return []
    audio_path = ctx.artifact_path(audio)
    return detect_silence_windows(audio_path)


def _safe_cut_boundaries(boundary_entries: list[dict]) -> list[dict]:
    """Frame-quantize the ordered semantic/audio boundary entries into safe cut points.

    ``boundary_entries`` already spans the whole timeline (it is seeded with the
    ``timeline_start`` / ``timeline_end`` boundaries), so this is a pure mapping onto the
    30fps grid — the frame index is ``floor(t*fps + 0.5)`` via the single grid source.
    """
    cuts: list[dict] = []
    for index, entry in enumerate(boundary_entries):
        time = round(float(entry.get("boundary", 0.0)), 3)
        semantic = entry.get("semantic_boundary")
        cuts.append(
            {
                "cut_id": f"cut_{index:03d}",
                "time": time,
                "frame": frame_index(time),
                "source": entry.get("boundary_source"),
                "reason": entry.get("reason"),
                "after_unit_id": entry.get("unit_id"),
                "semantic_time": (
                    round(float(semantic), 3) if semantic is not None else None
                ),
            }
        )
    return cuts


def _portrait_slots(
    safe_cut_boundaries: list[dict], planner_units: list[NarrationUnit]
) -> list[dict]:
    """One main-track window per consecutive pair of safe cuts (frame-contiguous).

    Each slot carries the narration units whose midpoint falls inside its time span, so a
    downstream filler knows which spoken content the window covers. Adjacent slots share a
    frame index (slot i ends on the frame slot i+1 starts on): the same no-gap/no-overlap
    invariant the render grid relies on.
    """
    slots: list[dict] = []
    for index in range(len(safe_cut_boundaries) - 1):
        start = safe_cut_boundaries[index]
        end = safe_cut_boundaries[index + 1]
        unit_ids = [
            unit.unit_id
            for unit in planner_units
            if start["time"] <= (unit.start + unit.end) / 2 < end["time"]
        ]
        slots.append(
            {
                "slot_id": f"pslot_{index:03d}",
                "start_cut_id": start["cut_id"],
                "end_cut_id": end["cut_id"],
                "start_frame": start["frame"],
                "end_frame": end["frame"],
                "unit_ids": unit_ids,
                "boundary_source": end["source"],
            }
        )
    return slots


def _broll_slots(planner_units: list[NarrationUnit], *, total_frames: int) -> list[dict]:
    """Per-narration-unit windows where B-roll may overlay (available windows, not a plan).

    These are the frame-clamped spans of each spoken unit plus its text; the actual B-roll
    placement is still decided downstream by TimelineWindowPlanning + the editing planners.
    Degenerate (< 1 frame) spans are dropped.
    """
    slots: list[dict] = []
    for index, unit in enumerate(planner_units):
        start_frame = max(0, frame_index(unit.start))
        end_frame = min(total_frames, frame_index(unit.end))
        if end_frame - start_frame < 1:
            continue
        slots.append(
            {
                "slot_id": f"bslot_{index:03d}",
                "start_frame": start_frame,
                "end_frame": end_frame,
                "unit_ids": [unit.unit_id],
                "text": unit.text,
            }
        )
    return slots
