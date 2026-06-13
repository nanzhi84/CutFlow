"""Deterministic assembly helpers for the V4 pipeline (no VLM, no IO).

Split out of ``pipeline`` to keep the orchestrator focused on flow + retries.
These are pure functions over sensor signals / VLM clips:

- window-internal frame-sample-time selection (deterministic hot-spot sampling);
- clip-boundary refine (cut-precision defences 2/3/4: snap -> inset -> internal split);
- aggregation of quality events / usage windows / evidence frames.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from packages.core.contracts import ClipV4, QualityEventV4, UsageRole, UsageWindowV4

from . import boundary as clip_boundary

logger = logging.getLogger("packages.media.annotation.assemble")


def is_portrait(material_type: str) -> bool:
    mt = str(material_type or "").strip().lower()
    return any(tok in mt for tok in ("portrait", "口播", "talk"))


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Frame sampling
# ---------------------------------------------------------------------------
def pick_window_sample_times(
    *,
    window_start: float,
    window_end: float,
    material_type: str,
    sensor_signals: dict[str, Any],
    budget: int,
    edge_window: float,
) -> list[float]:
    """Pick deterministic sample times: uniform fill + cuts + speech edges + window edges + quality hot-spots."""
    span = max(0.0, window_end - window_start)
    if span <= 0:
        return [round(window_start, 3)]

    candidates: list[float] = []

    n_uniform = max(2, budget)
    for i in range(n_uniform):
        frac = (i + 0.5) / n_uniform
        candidates.append(window_start + frac * span)

    for cut in sensor_signals.get("shot_cuts", []) or []:
        if window_start < cut < window_end:
            candidates.append(float(cut))

    for isl in sensor_signals.get("speech_islands", []) or []:
        for key in ("start", "end"):
            pos = safe_float(isl.get(key))
            if pos is not None and window_start < pos < window_end:
                candidates.append(pos)

    edge = min(edge_window, span / 2.0)
    if edge > 0:
        candidates.append(window_start + min(0.05, edge))
        candidates.append(window_end - min(0.05, edge))

    for ev in sensor_signals.get("quality_events", []) or []:
        s = safe_float(ev.get("start"))
        e = safe_float(ev.get("end"))
        if s is not None and e is not None and e > s:
            mid = (s + e) / 2.0
            if window_start < mid < window_end:
                candidates.append(mid)

    seen: list[float] = []
    for t in sorted(candidates):
        t = min(max(t, window_start), window_end)
        rt = round(t, 3)
        if not seen or abs(rt - seen[-1]) > 1e-3:
            seen.append(rt)
    # B-roll caps frame count (cuts/quality candidates are unbounded); portrait
    # keeps all (its density is unchanged, truncation would alter VLM input).
    if is_portrait(material_type):
        return seen
    cap = budget + 10
    if len(seen) > cap > 1:
        last_idx = len(seen) - 1
        picked = [round(i * last_idx / (cap - 1)) for i in range(cap)]
        seen = [seen[idx] for idx in picked]
    return seen


# ---------------------------------------------------------------------------
# Boundary refine (cut-precision defences 2/3/4)
# ---------------------------------------------------------------------------
def refine_clip_boundaries(
    clips: list[ClipV4],
    shot_cuts: list[float],
    duration: float,
    *,
    material_type: str,
    fps_assumed: float,
    inset_frames: int,
    snap_tol: float,
    internal_cut_edge_guard: float,
) -> list[ClipV4]:
    """For each clip: snap to real cuts -> safety inset (None drops it) -> internal self-check.

    Defence 1 (precise cuts) is the sensor layer; this applies 2/3/4. b-roll splits
    a clip that hides internal cuts into N+1 pieces; portrait drops the whole clip.
    """
    refined: list[ClipV4] = []
    for clip in clips:
        snapped_start, snapped_end = clip_boundary.snap_to_cuts(
            clip.start, clip.end, shot_cuts, tol=snap_tol
        )
        inset_bounds = clip_boundary.apply_safety_inset(
            snapped_start, snapped_end, fps=fps_assumed, inset_frames=inset_frames
        )
        if inset_bounds is None:
            continue
        new_start, new_end = inset_bounds
        inner_low = new_start + internal_cut_edge_guard
        inner_high = new_end - internal_cut_edge_guard
        internal_cuts = (
            sorted(float(c) for c in shot_cuts if inner_low < c < inner_high)
            if inner_high > inner_low
            else []
        )
        if not internal_cuts:
            if duration and duration > 0:
                new_end = min(new_end, float(duration))
            if new_end <= new_start:
                continue
            refined.append(clip_with_bounds(clip, new_start, new_end))
            continue
        if is_portrait(material_type):
            continue
        inset_sec = inset_frames / fps_assumed if inset_frames > 0 and fps_assumed > 0 else 0.0
        pieces: list[tuple[float, float]] = []
        piece_start = new_start
        for cut in internal_cuts:
            pieces.append((piece_start, cut - inset_sec))
            piece_start = cut + inset_sec
        pieces.append((piece_start, new_end))
        for i, (piece_s, piece_e) in enumerate(pieces, start=1):
            if duration and duration > 0:
                piece_e = min(piece_e, float(duration))
            if piece_e <= piece_s:
                continue
            refined.append(
                clip_with_bounds(clip, piece_s, piece_e, segment_id=f"{clip.segment_id}-s{i}")
            )
    return refined


def clip_with_bounds(
    clip: ClipV4,
    start: float,
    end: float,
    *,
    segment_id: str | None = None,
) -> ClipV4:
    """Copy a clip with replaced time bounds (duration self-corrects via the validator)."""
    payload = clip.model_dump()
    payload["start"] = round(float(start), 3)
    payload["end"] = round(float(end), 3)
    payload["duration"] = round(float(end) - float(start), 3)
    if segment_id is not None:
        payload["segment_id"] = segment_id
    return ClipV4.model_validate(payload)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def assemble_quality_events(cv_events: list[dict], duration: float) -> list[QualityEventV4]:
    """Wrap sensor CV events into QualityEventV4, adding event_id/source and clamping end."""
    upper = float(duration) if duration and duration > 0 else None
    events: list[QualityEventV4] = []
    for raw in cv_events or []:
        try:
            payload = dict(raw)
            payload.setdefault("event_id", f"qe-{uuid.uuid4().hex[:10]}")
            payload.setdefault("source", "sensor")
            if upper is not None and isinstance(payload.get("end"), (int, float)):
                payload["end"] = min(float(payload["end"]), upper)
            events.append(QualityEventV4.model_validate(payload))
        except Exception as exc:
            logger.debug("[V4] skipping invalid quality event: %s (%s)", raw, exc)
            continue
    events.sort(key=lambda e: e.start)
    return events


def build_usage_windows(clips: list[ClipV4]) -> list[UsageWindowV4]:
    """Generate recommended windows from clips whose role != avoid."""
    windows: list[UsageWindowV4] = []
    for clip in clips:
        role = clip.usage.role
        if role == UsageRole.avoid:
            continue
        reason = clip.retrieval.retrieval_sentence or clip.retrieval.summary or ""
        windows.append(
            UsageWindowV4(
                start=clip.start,
                end=clip.end,
                role=role,
                reason=reason,
                confidence=clip.confidence,
            )
        )
    return windows


def collect_evidence_frames(clips: list[ClipV4], duration: float) -> list[float]:
    """Take each clip midpoint as an evidence frame; clamp to [0, duration], dedup, ascending."""
    upper = float(duration) if duration and duration > 0 else None
    out: list[float] = []
    for clip in clips:
        mid = round((clip.start + clip.end) / 2.0, 3)
        if upper is not None:
            mid = min(max(mid, 0.0), upper)
        if not out or abs(mid - out[-1]) > 1e-3:
            out.append(mid)
    return sorted(set(out))
