"""Single 30fps frame grid: the one source of truth for timeline boundary quantization.

Ported faithfully from digital-human-Cutagent editing_agent/frame_grid.py. The
production bug this guards against: planned boundaries (e.g. 12.96s) that are not on
the 30fps grid cause downstream trim (concat / render) to pick a frame one short or
one long, and the rounding error accumulates along the timeline until adjacent
segments overlap or duplicate a frame at the junction (the seam flashes one frame).

Fix: at plan time every boundary is quantized ONCE onto a single 30fps frame grid,
and all slicing is by integer frame index so each window is exactly ``B - A`` frames
and adjacent windows are exactly contiguous (window i ends on the same frame index
that window i+1 starts on — zero overlap, zero gap, zero duplicated frame).

``TIMELINE_FPS`` is hard-wired to 30 (not configurable): the physical render rate is
fixed at 30 across the whole chain, so a configurable plan grid would only create a
new "plan grid != physical fps" mismatch surface. This module is zero-dependency
(pure functions) so any downstream slicer can import it safely.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TIMELINE_FPS = 30


def frame_index(t: float) -> int:
    """Seconds -> frame index: round-half-up (deterministic, monotone).

    Uses ``floor(t*fps + 0.5)`` rather than the builtin ``round()``: banker's
    rounding rounds .5 to even, which makes adjacent boundaries round in
    inconsistent directions. This is the CRITICAL INVARIANT — do not swap for
    round(). At an exact half-frame (e.g. t*fps == 12.5) floor(.+0.5) -> 13
    whereas round() -> 12, and that one-frame difference is what causes the seam.
    """
    return max(0, int(math.floor(float(t) * TIMELINE_FPS + 0.5)))


def to_seconds(frame: int) -> float:
    """Frame index -> seconds: the exact grid value ``frame / 30`` (no re-rounding)."""
    return int(frame) / TIMELINE_FPS


@dataclass(frozen=True)
class FrameWindow:
    """A half-open frame window ``[start_frame, end_frame)`` on the single grid.

    ``length_frames == end_frame - start_frame`` exactly.
    """

    start_frame: int
    end_frame: int

    @property
    def length_frames(self) -> int:
        return self.end_frame - self.start_frame


def slice_source_window(
    *,
    source_start_seconds: float,
    length_frames: int,
    source_window_start_seconds: float | None = None,
    source_window_end_seconds: float | None = None,
) -> tuple[FrameWindow, int]:
    """Frame-exact source slice covering ``length_frames`` from a source start.

    Returns ``(FrameWindow, pad_end_frames)``. The source start is snapped to the
    grid; the slice is exactly ``length_frames`` long so it matches the timeline
    window frame-for-frame. If the slice would overrun a declared source safety
    window (a single continuous shot), we first shift the start earlier within the
    available head-room (content moves <= ~1.5 frames, imperceptible), then — only
    if head-room is exhausted — shrink the real content and report the shortfall as
    ``pad_end_frames`` (freeze the last frame to fill the window). We never bare-clamp
    the end short: missing frames would shift every later junction (the old bug).
    """
    source_start_frame = frame_index(source_start_seconds)
    content_frames = max(0, int(length_frames))
    if source_window_end_seconds is not None and content_frames > 0:
        window_end_frame = frame_index(source_window_end_seconds)
        overshoot = source_start_frame + content_frames - window_end_frame
        if overshoot > 0:
            window_floor_frame = (
                frame_index(source_window_start_seconds)
                if source_window_start_seconds is not None
                else 0
            )
            headroom = max(0, source_start_frame - window_floor_frame)
            shift = min(overshoot, headroom)
            source_start_frame -= shift
            overshoot -= shift
            if overshoot > 0:
                content_frames = max(1, content_frames - overshoot)
    pad_end_frames = max(0, int(length_frames) - content_frames)
    return (
        FrameWindow(
            start_frame=source_start_frame,
            end_frame=source_start_frame + content_frames,
        ),
        pad_end_frames,
    )
