"""Re-clip a stored AnnotationV4 canonical to a new media duration.

Used by replace-source: when the replacement media's duration drifts from the
annotated one, the preserved annotation's time-bearing layers (clips /
usage_windows / quality_events / evidence_frames) would otherwise point past or
into the wrong frames.

- endpoints within ``endpoint_tolerance`` of the old duration snap to the new
  duration (a segment that ended exactly at end-of-video keeps ending at
  end-of-video);
- every start/end is then clamped to ``[0, new_duration]``;
- zero-length / inverted segments after clamping are dropped (never emitted with
  end<=start, which the strict ClipV4 / QualityEventV4 validators would reject).

Pure + deterministic: takes a canonical dict, returns a NEW canonical dict that is
guaranteed to re-validate as :class:`AnnotationV4` against the new duration.
"""

from __future__ import annotations

from typing import Any

from packages.core.contracts import AnnotationV4

DEFAULT_DURATION_DRIFT_THRESHOLD = 0.15


def _snap_clamp(value: float, old_duration: float, new_duration: float, tol: float) -> float:
    if old_duration > 0 and abs(value - old_duration) <= tol:
        value = new_duration
    return max(0.0, min(float(value), new_duration))


def reclip_canonical_to_duration(
    canonical: dict[str, Any],
    *,
    old_duration: float,
    new_duration: float,
    endpoint_tolerance: float = 0.2,
) -> dict[str, Any] | None:
    """Return a NEW canonical re-clipped to ``new_duration``, or ``None`` when not V4.

    Out-of-bounds / collapsed time layers are clamped/dropped so the result always
    re-validates as AnnotationV4 against ``new_duration``. ``None`` signals the caller
    that the stored canonical is not a real AnnotationV4 (a labels-only stub) or is
    a BGM annotation whose segment semantics must be regenerated from the new audio.
    """
    meta = canonical.get("meta") if isinstance(canonical, dict) else None
    if not (isinstance(meta, dict) and "asset_id" in meta):
        return None
    if str(meta.get("material_type") or "").lower() == "bgm":
        return None

    new_canonical = dict(canonical)
    new_meta = dict(meta)
    new_meta["duration"] = round(float(new_duration), 6)
    new_canonical["meta"] = new_meta

    new_canonical["clips"] = _reclip_intervals(
        canonical.get("clips"), old_duration, new_duration, endpoint_tolerance, with_duration=True
    )
    new_canonical["quality_events"] = _reclip_intervals(
        canonical.get("quality_events"), old_duration, new_duration, endpoint_tolerance, with_duration=False
    )
    new_canonical["usage_windows"] = _reclip_intervals(
        canonical.get("usage_windows"), old_duration, new_duration, endpoint_tolerance, with_duration=False
    )
    frames = canonical.get("evidence_frames")
    if isinstance(frames, list):
        new_canonical["evidence_frames"] = [
            round(_snap_clamp(float(ts), old_duration, new_duration, endpoint_tolerance), 6)
            for ts in frames
            if isinstance(ts, (int, float))
            and 0.0 <= _snap_clamp(float(ts), old_duration, new_duration, endpoint_tolerance) <= new_duration
        ]
    return new_canonical


def _reclip_intervals(
    items: Any,
    old_duration: float,
    new_duration: float,
    tol: float,
    *,
    with_duration: bool,
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        try:
            start = _snap_clamp(float(item.get("start", 0.0)), old_duration, new_duration, tol)
            end = _snap_clamp(float(item.get("end", 0.0)), old_duration, new_duration, tol)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        item["start"] = round(start, 6)
        item["end"] = round(end, 6)
        if with_duration:
            item["duration"] = round(end - start, 6)
        out.append(item)
    return out


def reclipped_or_validated(
    canonical: dict[str, Any],
    *,
    old_duration: float,
    new_duration: float,
) -> dict[str, Any] | None:
    """Re-clip ``canonical`` to ``new_duration`` and confirm it re-validates as AnnotationV4.

    Returns the re-clipped canonical dict, or ``None`` when it is not a V4 canonical or
    the re-clipped form still fails strict validation (caller then invalidates the
    annotation rather than keep a broken one).
    """
    reclipped = reclip_canonical_to_duration(
        canonical, old_duration=old_duration, new_duration=new_duration
    )
    if reclipped is None:
        return None
    try:
        AnnotationV4.model_validate(reclipped)
    except Exception:
        return None
    return reclipped
