"""Deterministic white/yellow phrase segmentation for caption overlays."""

from __future__ import annotations

import re

_PRICE_OR_NUMBER = re.compile(
    r"(?:¥|￥|\$)?\d+(?:\.\d+)?(?:万|千|百|元|块|折|%|％|米|公里|小时|分钟|秒|人|次|个|张|套|斤)?"
)
_EXPLICIT_KEYWORDS = (
    "免费",
    "限时",
    "直达",
    "核心",
    "必看",
    "唯一",
    "立即",
    "出发",
    "门票",
    "地址",
    "地点",
    "省时",
    "省钱",
    "黄金",
)


def highlighted_spans(text: str) -> list[tuple[str, bool]]:
    """Return ordered text spans with deterministic highlight flags."""

    value = str(text or "")
    numeric_ranges = [(match.start(), match.end()) for match in _PRICE_OR_NUMBER.finditer(value)]
    ranges = list(numeric_ranges)
    for keyword in _EXPLICIT_KEYWORDS:
        cursor = 0
        while True:
            index = value.find(keyword, cursor)
            if index < 0:
                break
            keyword_range = (index, index + len(keyword))
            # Keep an adjacent price/quantity as the single yellow focal span so
            # phrases such as "限时2000元" remain visibly white + yellow.
            if not any(
                keyword_range[0] <= number_end and number_start <= keyword_range[1]
                for number_start, number_end in numeric_ranges
            ):
                ranges.append(keyword_range)
            cursor = index + len(keyword)
    if not ranges:
        return [(value, False)] if value else []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    spans: list[tuple[str, bool]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            spans.append((value[cursor:start], False))
        spans.append((value[start:end], True))
        cursor = end
    if cursor < len(value):
        spans.append((value[cursor:], False))
    return spans
