"""Deterministic jieba keyword extraction.

Ported from the origin ``broll_clip_agent`` (``_extract_script_keywords`` /
``_extract_scene_keywords``). Pure functions, no IO and no randomness: jieba's POS
tagging is deterministic on a fixed dictionary, so two identical runs produce
identical keyword sets. This is the matching substrate consumed by :mod:`matching`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import jieba.posseg as pseg

# POS prefixes kept as content keywords (noun / verb families). Mirrors the
# origin: n=noun, v=verb, ns=place, nz=proper, vn=verbal-noun, an=adj-noun.
_KEPT_POS = ("n", "v", "ns", "nz", "vn", "an")
_MIN_TOKEN_LEN = 2
_MAX_TOKEN_LEN = 10
_MAX_SCRIPT_KEYWORDS = 20
# Scene cues that are meaningful even when jieba does not segment them as a
# single token (kept from the origin's preset scene-keyword list).
_SCENE_CUES = (
    "开场",
    "产品介绍",
    "效果展示",
    "施工过程",
    "客户评价",
    "结尾",
    "价格",
    "优惠",
    "活动",
)


@dataclass(frozen=True)
class ScriptSegment:
    """A script beat with its estimated time window and extracted keywords."""

    text: str
    start: float
    end: float
    keywords: tuple[str, ...] = field(default_factory=tuple)


def extract_keywords(text: str) -> list[str]:
    """Extract ordered, de-duplicated content keywords from ``text`` via jieba.

    Deterministic: keeps noun/verb-family tokens of length 2..10, appends any
    preset scene cue present in the text, and preserves first-seen order.
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return []

    keywords: list[str] = []
    for word, flag in pseg.cut(cleaned):
        token = str(word).strip()
        if not token:
            continue
        if flag.startswith(_KEPT_POS) and len(token) >= _MIN_TOKEN_LEN:
            keywords.append(token)

    for cue in _SCENE_CUES:
        if cue in cleaned:
            keywords.append(cue)

    seen: set[str] = set()
    unique: list[str] = []
    for token in keywords:
        if token in seen or len(token) > _MAX_TOKEN_LEN:
            continue
        seen.add(token)
        unique.append(token)
    return unique[:_MAX_SCRIPT_KEYWORDS]


