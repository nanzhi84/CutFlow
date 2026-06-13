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

# Max allowed deviation (seconds) between a quantized boundary and the grid. The
# float error of round(t*30)/30 is ~1e-14; 1e-6 leaves ample margin while staying
# far below half a frame (16.7ms).
GRID_EPSILON = 1e-6


def frame_index(t: float) -> int:
    """Seconds -> frame index: round-half-up (deterministic, monotone).

    Uses ``floor(t*fps + 0.5)`` rather than the builtin ``round()``: banker's
    rounding rounds .5 to even, which makes adjacent boundaries round in
    inconsistent directions. This is the CRITICAL INVARIANT — do not swap for
    round(). At an exact half-frame (e.g. t*fps == 12.5) floor(.+0.5) -> 13
    whereas round() -> 12, and that one-frame difference is what causes the seam.
    """
    return max(0, int(math.floor(float(t) * TIMELINE_FPS + 0.5)))


def to_frame(t: float) -> int:
    """Alias of :func:`frame_index` for call sites that read better as ``to_frame``."""
    return frame_index(t)


def to_seconds(frame: int) -> float:
    """Frame index -> seconds: the exact grid value ``frame / 30`` (no re-rounding)."""
    return int(frame) / TIMELINE_FPS


def quantize_boundary(t: float) -> float:
    """Snap a time boundary onto the 30fps grid (returns the exact ``k/30`` float).

    Deliberately does NOT round to 3 decimals afterwards: 0.0333 differs from 1/30
    by 3e-4, and a second rounding would push the boundary off the grid. We store
    the full-precision ``k/30``; :func:`frame_index` still recovers ``k`` losslessly.
    """
    return frame_index(t) / TIMELINE_FPS


def frame_count(duration: float) -> int:
    """Duration (seconds) -> frame count, same round-half-up rule as frame_index."""
    return max(0, int(math.floor(float(duration) * TIMELINE_FPS + 0.5)))


def is_on_grid(t: float, *, epsilon: float = GRID_EPSILON) -> bool:
    """True if ``t`` already sits on the frame grid (|t*30 - round(t*30)| < eps)."""
    scaled = float(t) * TIMELINE_FPS
    return abs(scaled - math.floor(scaled + 0.5)) < epsilon * TIMELINE_FPS


@dataclass(frozen=True)
class FrameWindow:
    """A half-open frame window ``[start_frame, end_frame)`` on the single grid.

    ``length_frames == end_frame - start_frame`` exactly; ``start_seconds`` /
    ``end_seconds`` are the exact grid values. Adjacent windows produced by
    :func:`slice_windows` satisfy ``prev.end_frame == next.start_frame``.
    """

    start_frame: int
    end_frame: int

    @property
    def length_frames(self) -> int:
        return self.end_frame - self.start_frame

    @property
    def start_seconds(self) -> float:
        return to_seconds(self.start_frame)

    @property
    def end_seconds(self) -> float:
        return to_seconds(self.end_frame)

    @property
    def duration_seconds(self) -> float:
        return self.length_frames / TIMELINE_FPS


def slice_windows(boundaries_seconds: list[float]) -> list[FrameWindow]:
    """Turn a sorted boundary list into exact, contiguous frame windows.

    Each input boundary is quantized to a single grid frame index ONCE; window i is
    ``[frame(b_i), frame(b_{i+1}))``. Because window i's end frame is literally
    window i+1's start frame, adjacent windows never overlap and never duplicate a
    frame. Windows that quantize to < 1 frame (degenerate) are dropped and their
    span is absorbed by the following window (its start pulls back to the dropped
    boundary's frame), so total length is preserved.
    """
    if len(boundaries_seconds) < 2:
        return []
    frames = [frame_index(b) for b in boundaries_seconds]
    windows: list[FrameWindow] = []
    pending_start = frames[0]
    for end_frame in frames[1:]:
        end_frame = max(pending_start, end_frame)
        if end_frame - pending_start < 1:
            # Degenerate (< 1 frame): drop it; its span folds into the next window
            # (pending_start unchanged so the next real boundary starts here).
            continue
        windows.append(FrameWindow(start_frame=pending_start, end_frame=end_frame))
        pending_start = end_frame
    return windows


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
