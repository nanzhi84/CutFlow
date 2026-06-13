"""Top-level pure boundary/timeline planner: the editing-agent step-2a entry point.

A pure function:

    plan_boundary_timeline(narration_units, portrait_candidates, fps, constraints,
                           [audio_pauses]) -> BoundaryTimelinePlan

It runs the semantic+capacity boundary planner (packing.py) to choose portrait
source windows for boundary-locked chunks, then quantizes the WHOLE timeline ONCE
onto the single 30fps frame grid (frame_grid.py) so:
  - every boundary maps to a single frame index via floor(t*fps + 0.5);
  - timeline windows are frame-exact and adjacent windows are contiguous
    (window i ends on the same frame index window i+1 starts on — no overlap, no
    duplicated frame at the junction);
  - each source slice is exactly ``B - A`` frames (the timeline window's length),
    shifted / pad-frozen inside its source safety window instead of bare-clamped.

Audio pauses are OPTIONAL: when ``audio_pauses`` is given the boundary planner snaps
cuts into real silences; when omitted it falls back to SEMANTIC-ONLY boundaries.
NO ffmpeg / audio detection here — pauses are an input, never computed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.editing import packing
from packages.planning.editing import _util as util
from packages.planning.editing.constants import BOUNDARY_BEAM_WIDTH
from packages.planning.editing.frame_grid import (
    TIMELINE_FPS,
    FrameWindow,
    frame_index,
    slice_source_window,
)


@dataclass(frozen=True)
class BoundaryConstraints:
    """Caller-supplied planner constraints."""

    target_duration: float
    max_chunk_duration: float | None = None
    include_unlimited_reuse_scope: bool = True
    beam_width: int = BOUNDARY_BEAM_WIDTH


@dataclass(frozen=True)
class PlannedSegment:
    """One portrait segment, fully frame-quantized on the single grid."""

    index: int
    template_id: str
    window_id: str
    timeline_start_frame: int
    timeline_end_frame: int
    source_start_frame: int
    source_end_frame: int
    source_pad_end_frames: int
    role: str | None
    phase: str | None
    source_mode: str
    boundary_source: str | None
    boundary_reason: str | None
    unit_ids: list[str]
    reuse_index: int
    material_reuse_fallback: bool

    @property
    def timeline_length_frames(self) -> int:
        return self.timeline_end_frame - self.timeline_start_frame

    @property
    def timeline_start_seconds(self) -> float:
        return self.timeline_start_frame / TIMELINE_FPS

    @property
    def timeline_end_seconds(self) -> float:
        return self.timeline_end_frame / TIMELINE_FPS


@dataclass(frozen=True)
class BoundaryTimelinePlan:
    """The frame-quantized portrait boundary/timeline plan (planner output)."""

    fps: int
    total_frames: int
    segments: list[PlannedSegment]
    used_audio_pauses: bool
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.segments)


def plan_boundary_timeline(
    *,
    narration_units: list[NarrationUnit],
    portrait_candidates: list[dict[str, Any]],
    constraints: BoundaryConstraints,
    audio_pauses: list[dict[str, float]] | None = None,
    fps: int = TIMELINE_FPS,
) -> BoundaryTimelinePlan:
    """Plan portrait boundaries + windows, then quantize once onto the 30fps grid.

    ``fps`` is accepted for caller clarity but the grid is fixed at 30 (the physical
    render rate) — see frame_grid.TIMELINE_FPS. A mismatch raises, so the single
    source of truth is never silently diverged from.
    """
    if int(fps) != TIMELINE_FPS:
        raise ValueError(
            f"plan_boundary_timeline only supports the fixed {TIMELINE_FPS}fps grid, got fps={fps}"
        )

    plan_segments, trace = packing.build_boundary_locked_portrait_plan(
        portrait_candidates=portrait_candidates,
        narration_units=narration_units,
        target_duration=constraints.target_duration,
        pause_windows=audio_pauses or None,
        max_chunk_duration=constraints.max_chunk_duration,
        include_unlimited_reuse_scope=constraints.include_unlimited_reuse_scope,
        beam_width=constraints.beam_width,
    )
    used_audio_pauses = bool(audio_pauses)
    if not plan_segments:
        total_frames = max(0, _frame(constraints.target_duration))
        return BoundaryTimelinePlan(
            fps=TIMELINE_FPS,
            total_frames=total_frames,
            segments=[],
            used_audio_pauses=used_audio_pauses,
            trace=trace,
        )

    ordered = sorted(plan_segments, key=lambda seg: util.as_float(seg.get("timeline_start"), 0.0))
    return _quantize_plan(ordered, used_audio_pauses=used_audio_pauses, trace=trace)


def _frame(t: float) -> int:
    from packages.planning.editing.frame_grid import frame_index

    return frame_index(t)


def _quantize_plan(
    ordered: list[dict[str, Any]],
    *,
    used_audio_pauses: bool,
    trace: list[dict[str, Any]],
) -> BoundaryTimelinePlan:
    """Quantize the timeline ONCE: one boundary list -> contiguous frame windows.

    The plan's contiguous timeline boundaries (each segment's start == previous
    segment's end) are collected into a single sorted boundary list and quantized in
    one pass via frame_index; each segment is then paired with its own
    ``[frame(b_i), frame(b_{i+1}))`` window, so adjacent windows share a frame index
    and never overlap. A segment whose span quantizes to < 1 frame is dropped (and
    recorded in the trace). Source spans are then sliced to exactly the timeline
    window length.
    """
    boundaries_seconds = [util.as_float(ordered[0].get("timeline_start"), 0.0)]
    for seg in ordered:
        boundaries_seconds.append(util.as_float(seg.get("timeline_end"), 0.0))

    # Quantize every timeline boundary ONCE onto the single 30fps grid, then pair each
    # segment with its OWN window [frame(b_i), frame(b_{i+1})). Adjacent windows are
    # contiguous (segment i's end frame == segment i+1's start frame). A segment whose
    # span quantizes to < 1 frame is degenerate (zero-length on the grid): it is
    # dropped here (and recorded in the trace) rather than (a) silently shifting later
    # segments onto the wrong window -- the old next(window_iter)+break bug -- or
    # (b) hard-crashing the render. The chunk builder's >0.08s (>=~3 frame) floor means
    # this never fires on the real pipeline; the guard stays correct under `python -O`
    # (no assert) if a future change loosens that floor.
    boundary_frames = [frame_index(b) for b in boundaries_seconds]
    segments: list[PlannedSegment] = []
    for seg_index, seg in enumerate(ordered):
        start_frame = boundary_frames[seg_index]
        end_frame = max(start_frame, boundary_frames[seg_index + 1])
        if end_frame - start_frame < 1:
            trace.append(
                {
                    "event": "degenerate_segment_dropped",
                    "timeline_start": seg.get("timeline_start"),
                    "timeline_end": seg.get("timeline_end"),
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                }
            )
            continue
        window = FrameWindow(start_frame=start_frame, end_frame=end_frame)
        source_window, pad_end_frames = slice_source_window(
            source_start_seconds=util.as_float(seg.get("source_start"), 0.0),
            length_frames=window.length_frames,
            source_window_start_seconds=_opt_float(seg.get("source_window_start")),
            source_window_end_seconds=_opt_float(seg.get("source_window_end")),
        )
        segments.append(
            PlannedSegment(
                index=len(segments) + 1,
                template_id=str(seg.get("template_id") or "").strip(),
                window_id=str(seg.get("selected_candidate_id") or "").strip(),
                timeline_start_frame=window.start_frame,
                timeline_end_frame=window.end_frame,
                source_start_frame=source_window.start_frame,
                source_end_frame=source_window.end_frame,
                source_pad_end_frames=pad_end_frames,
                role=seg.get("role"),
                phase=seg.get("slot_phase"),
                source_mode=str(seg.get("source_mode") or "lipsynced"),
                boundary_source=seg.get("boundary_source"),
                boundary_reason=seg.get("boundary_reason"),
                unit_ids=list(seg.get("unit_ids") or []),
                reuse_index=int(seg.get("source_window_reuse_index", 0) or 0),
                material_reuse_fallback=bool(seg.get("material_reuse_fallback")),
            )
        )

    total_frames = segments[-1].timeline_end_frame if segments else 0
    return BoundaryTimelinePlan(
        fps=TIMELINE_FPS,
        total_frames=total_frames,
        segments=segments,
        used_audio_pauses=used_audio_pauses,
        trace=trace,
    )


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
