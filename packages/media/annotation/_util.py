"""Dependency-free leaf helpers shared by the annotation sensor suite.

These are deterministic numeric / coercion utilities used by the quality report
aggregator and the motion guard. Kept private to the annotation package.
"""

from __future__ import annotations

from typing import Any

# Time rounding precision used across sensors: 3 decimals (~millisecond) is
# enough for annotation and absorbs floating-point jitter.
TIME_DECIMALS = 3


def to_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float conversion; non-numeric input falls back to ``default``."""
    try:
        return float(value)
    except Exception:
        return default


# Alias kept for the motion guard's verbatim metric-reading helpers.
as_float = to_float


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def overlap_duration(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    """Overlap duration between two ranges (0 when disjoint)."""
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def is_explicit_false(value: Any) -> bool:
    """True only for an explicit falsey signal (bool False or a 'no'-like string).

    None / missing is NOT treated as explicit false (absence of evidence is not
    evidence of absence).
    """
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "0", "no", "否", "不", "不是"}
    return False
