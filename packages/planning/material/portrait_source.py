"""Portrait source-window helpers shared by shortlist, agent, and materialize."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.planning.material._avoid import subtract_bad_spans

PORTRAIT_CLEAN_SPAN_MIN_SECONDS = 0.08


def longest_clean_portrait_source_span(
    metadata: Mapping[str, Any] | None,
) -> tuple[float, float] | None:
    """Return the longest clean portrait source span from candidate metadata.

    The policy intentionally matches material-pack source-window cleaning: subtract
    ``avoid_spans`` with ``min_len=0.08``. When ``avoid_spans`` is missing or
    empty, a valid source window is returned unchanged.
    Ties choose the earliest clean start for deterministic materialization.
    """
    clean_spans = clean_portrait_source_windows(metadata)
    if not clean_spans:
        return None
    return max(clean_spans, key=lambda span: (span[1] - span[0], -span[0]))


def clean_portrait_source_windows(
    metadata: Mapping[str, Any] | None,
    *,
    source_duration: float | None = None,
) -> list[tuple[float, float]]:
    """Return every clean, lip-sync-usable source window described by metadata.

    ``source_duration`` is optional so pure planning callers can operate on already
    bounded metadata. Production MaterialPackPlanning passes it when available to
    clamp annotation windows before subtracting hard avoid spans.
    """
    if metadata is None:
        return []
    try:
        source_start = max(0.0, float(metadata.get("source_start") or 0.0))
        source_end = float(metadata.get("source_end") or 0.0)
    except (TypeError, ValueError):
        return []
    if source_duration is not None:
        try:
            duration = round(float(source_duration), 3)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= PORTRAIT_CLEAN_SPAN_MIN_SECONDS:
            return []
        source_end = min(duration, source_end)
    if source_end - source_start <= PORTRAIT_CLEAN_SPAN_MIN_SECONDS:
        return []

    try:
        avoid_spans = [
            (float(start), float(end)) for start, end in (metadata.get("avoid_spans") or [])
        ]
    except (TypeError, ValueError):
        avoid_spans = []
    return subtract_bad_spans(
        source_start,
        source_end,
        avoid_spans,
        min_len=PORTRAIT_CLEAN_SPAN_MIN_SECONDS,
    )
