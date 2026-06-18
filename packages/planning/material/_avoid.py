from __future__ import annotations

from collections.abc import Iterable


def avoid_intervals(
    annotation,
    *,
    types=("shake", "camera_drop", "occlusion"),
    hard_only=True,
) -> list[tuple[float, float]]:
    """Return merged quality-event intervals that the selector should avoid."""
    avoid_types = {_enum_value(event_type) for event_type in types}
    intervals: list[tuple[float, float]] = []
    for event in getattr(annotation, "quality_events", ()) or ():
        if _enum_value(getattr(event, "event_type", "")) not in avoid_types:
            continue
        if hard_only and str(getattr(event, "risk_tier", "")).strip().lower() != "hard":
            continue
        start = round(float(getattr(event, "start", 0.0)), 3)
        end = round(float(getattr(event, "end", 0.0)), 3)
        if end > start:
            intervals.append((start, end))
    return _merge_intervals(intervals)


def subtract_bad_spans(
    start: float,
    end: float,
    bad: Iterable[tuple[float, float]],
    *,
    min_len: float,
) -> list[tuple[float, float]]:
    """Return clean sub-spans after subtracting bad intervals from ``[start, end]``.

    ``min_len`` only drops split remainders created by cutting around an
    overlapping bad interval. An untouched span (no overlapping bad) is returned
    whole regardless of its length, so a clip with no bad spans is never dropped
    here — this preserves the pre-avoidance candidate set exactly.
    """
    source_start = float(start)
    source_end = float(end)
    min_length = max(0.0, float(min_len))
    if source_end <= source_start:
        return []

    bad_intervals = _merge_intervals(bad)
    if not bad_intervals:
        return [(source_start, source_end)]
    overlapping_bad = [
        (bad_start, bad_end)
        for bad_start, bad_end in bad_intervals
        if min(source_end, bad_end) > max(source_start, bad_start)
    ]
    if not overlapping_bad:
        return [(source_start, source_end)]

    span_start = round(source_start, 3)
    span_end = round(source_end, 3)
    if span_end <= span_start:
        return []

    clean: list[tuple[float, float]] = []
    cursor = span_start
    for bad_start, bad_end in overlapping_bad:
        if bad_end <= cursor:
            continue
        if bad_start >= span_end:
            break
        clipped_bad_start = max(span_start, bad_start)
        clipped_bad_end = min(span_end, bad_end)
        if clipped_bad_start > cursor:
            _append_if_long_enough(clean, cursor, clipped_bad_start, min_length)
        cursor = max(cursor, clipped_bad_end)
        if cursor >= span_end:
            break

    if cursor < span_end:
        _append_if_long_enough(clean, cursor, span_end, min_length)
    return clean


def _enum_value(value) -> str:
    return str(getattr(value, "value", value))


def _merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    normalized = sorted(
        (round(float(start), 3), round(float(end), 3)) for start, end in intervals
    )
    merged: list[tuple[float, float]] = []
    for start, end in normalized:
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _append_if_long_enough(
    spans: list[tuple[float, float]], start: float, end: float, min_len: float
) -> None:
    span = (round(start, 3), round(end, 3))
    if span[1] - span[0] + 1e-9 >= min_len:
        spans.append(span)
