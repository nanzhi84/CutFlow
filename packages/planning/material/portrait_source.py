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

    The policy intentionally matches ``TimelineWindowPlanning``: subtract
    ``avoid_spans`` with ``min_len=0.08``. When ``avoid_spans`` is missing or
    empty, ``subtract_bad_spans`` returns the original source window unchanged.
    Ties choose the earliest clean start for deterministic materialization.
    """
    if metadata is None:
        return None
    try:
        source_start = float(metadata.get("source_start") or 0.0)
        source_end = float(metadata.get("source_end") or 0.0)
    except (TypeError, ValueError):
        return None
    if source_end <= source_start:
        return None

    try:
        avoid_spans = [
            (float(start), float(end)) for start, end in (metadata.get("avoid_spans") or [])
        ]
    except (TypeError, ValueError):
        avoid_spans = []
    clean_spans = subtract_bad_spans(
        source_start,
        source_end,
        avoid_spans,
        min_len=PORTRAIT_CLEAN_SPAN_MIN_SECONDS,
    )
    if not clean_spans:
        return None
    return max(clean_spans, key=lambda span: (span[1] - span[0], -span[0]))
