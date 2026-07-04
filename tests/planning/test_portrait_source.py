from __future__ import annotations

from packages.planning.material.portrait_source import (
    clean_portrait_source_windows,
    longest_clean_portrait_source_span,
)


def test_clean_portrait_source_windows_clamps_and_splits_avoid_spans():
    metadata = {
        "source_start": -1.0,
        "source_end": 8.0,
        "avoid_spans": [[2.0, 4.0]],
    }

    assert clean_portrait_source_windows(metadata, source_duration=6.0) == [
        (0.0, 2.0),
        (4.0, 6.0),
    ]


def test_longest_clean_portrait_source_span_chooses_longest_then_earliest():
    metadata = {
        "source_start": 0.0,
        "source_end": 8.0,
        "avoid_spans": [[2.0, 4.0], [6.0, 8.0]],
    }

    assert longest_clean_portrait_source_span(metadata) == (0.0, 2.0)
