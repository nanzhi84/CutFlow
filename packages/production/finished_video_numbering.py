from __future__ import annotations

from collections.abc import Iterable


def next_finished_video_number(existing_numbers: Iterable[str | None]) -> str:
    max_number = 0
    for value in existing_numbers:
        if isinstance(value, str) and value.startswith("V-") and value[2:].isdigit():
            max_number = max(max_number, int(value[2:]))
    return f"V-{max_number + 1:03d}"
