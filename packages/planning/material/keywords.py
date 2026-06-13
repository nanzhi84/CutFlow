"""Deterministic jieba keyword extraction + script segmentation.

Ported from the origin ``broll_clip_agent`` (``_extract_script_keywords`` /
``_segment_script_by_sentence`` / ``_extract_scene_keywords``). Pure functions,
no IO and no randomness: jieba's POS tagging is deterministic on a fixed
dictionary, so two identical runs produce identical keyword sets and segment
timings. This is the matching substrate consumed by :mod:`matching`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import jieba.posseg as pseg

# POS prefixes kept as content keywords (noun / verb families). Mirrors the
# origin: n=noun, v=verb, ns=place, nz=proper, vn=verbal-noun, an=adj-noun.
_KEPT_POS = ("n", "v", "ns", "nz", "vn", "an")
_MIN_TOKEN_LEN = 2
_MAX_TOKEN_LEN = 10
_MAX_SCRIPT_KEYWORDS = 20
# Char-per-second estimate when no real narration timing is available. The
# matching layer prefers real NarrationUnit windows; this is only a fallback
# inside pure keyword helpers.
_CHARS_PER_SECOND = 4.0
_MIN_SEGMENT_SECONDS = 2.0

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

_SENTENCE_SPLIT = re.compile(r"[。！？!?;；\n]+")


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


def segment_script(text: str, *, keywords: list[str] | None = None) -> list[ScriptSegment]:
    """Split ``text`` into sentence beats with estimated time windows.

    Used only when no real NarrationUnit timing is available. Each beat's
    duration is estimated from its character count (>= 2s), matching the
    origin's ``_segment_script_by_sentence``.
    """
    vocab = keywords if keywords is not None else extract_keywords(text)
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(str(text or "")) if s.strip()]

    segments: list[ScriptSegment] = []
    cursor = 0.0
    for sentence in sentences:
        duration = max(len(sentence) / _CHARS_PER_SECOND, _MIN_SEGMENT_SECONDS)
        seg_keywords = tuple(kw for kw in vocab if kw in sentence)
        segments.append(
            ScriptSegment(
                text=sentence,
                start=round(cursor, 3),
                end=round(cursor + duration, 3),
                keywords=seg_keywords,
            )
        )
        cursor += duration
    return segments
