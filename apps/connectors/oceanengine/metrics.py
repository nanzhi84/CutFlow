"""Pure numeric/text parsing helpers for OceanEngine XLSX cells.

These mirror the original RPA importer semantics (currency, percent, and
thousands-separator handling) so that re-imports stay byte-stable and dedupe
fingerprints are reproducible.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_EMPTY_TOKENS = {"-", "--", "—", "无", "null", "None", ""}
_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


def normalize_text(value: Any) -> str:
    """Trim and stringify a cell value (``None`` becomes the empty string)."""

    if value is None:
        return ""
    return str(value).strip()


def parse_number(value: Any) -> float | None:
    """Parse a Chinese-locale numeric cell into a float.

    Handles thousands separators, currency markers (``元``/``￥``/``¥``),
    and percentages (``12%`` -> ``0.12``). Returns ``None`` for blank/placeholder
    cells so missing metrics are not silently coerced to zero.
    """

    text = normalize_text(value)
    if text in _EMPTY_TOKENS:
        return None
    is_percent = "%" in text
    cleaned = (
        text.replace(",", "")
        .replace("，", "")
        .replace("￥", "")
        .replace("¥", "")
        .replace("元", "")
        .replace("%", "")
        .strip()
    )
    match = _NUMBER_PATTERN.search(cleaned)
    if not match:
        return None
    number = float(match.group(0))
    return number / 100 if is_percent else number


def parse_int(value: Any) -> int | None:
    """Parse a cell into an int via :func:`parse_number` rounding."""

    number = parse_number(value)
    if number is None:
        return None
    return int(round(number))


def safe_divide(numerator: float | int | None, denominator: float | int | None) -> float | None:
    """Divide guarding against ``None`` and zero denominators."""

    if numerator is None or denominator in (None, 0):
        return None
    return round(float(numerator) / float(denominator), 6)


def pick(row: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty value among ``keys`` (alias-tolerant headers)."""

    for key in keys:
        if key in row and normalize_text(row[key]):
            return normalize_text(row[key])
    return ""


def canonical_json(value: Any) -> str:
    """Deterministic JSON used as the basis for content fingerprints."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_row(row: dict[str, Any]) -> str:
    """Stable content hash of a raw source row, used for per-row dedupe."""

    return sha256_text(canonical_json(row))
