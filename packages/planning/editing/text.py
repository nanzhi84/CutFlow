"""Pure narration / sentence text predicates (ported from editing_agent/narration_text.py).

No state. Used by the narration splitter to classify sentence ends and detect intent.
Behaviour is byte-identical to the origin's static helpers.
"""

from __future__ import annotations

import re

_HARD_ENDS = ("。", "！", "？", ".", "!", "?", "；", ";", "…")
_SOFT_ENDS = ("，", "、", "：", ",", ":")
_DEFAULT_READING_MAX_CHARS = 18
_DEFAULT_READING_MIN_CHARS = 8


def detect_narration_intent(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return "explain"
    if any(token in normalized for token in ("马上", "立即", "点击", "咨询", "预约", "留言", "私信")):
        return "cta"
    if any(token in normalized for token in ("效果", "结果", "对比", "前后", "修复", "改善")):
        return "proof"
    if any(token in normalized for token in ("为什么", "其实", "因为", "就是", "原理", "方法")):
        return "explain"
    if any(token in normalized for token in ("痛点", "麻烦", "问题", "困扰", "别再")):
        return "pain_point"
    return "explain"


def is_hard_sentence_end(text: str) -> bool:
    return str(text or "").rstrip().endswith(_HARD_ENDS)


def is_soft_sentence_end(text: str) -> bool:
    return str(text or "").rstrip().endswith(_SOFT_ENDS)


def clean_text_for_timing(text: str) -> str:
    return re.sub(r"[^\w一-鿿]", "", str(text or "")).lower()


def split_script_sentences(script: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", str(script or "")).strip()
    if not normalized:
        return []

    end_tokens = "。！？!?；;…"
    closing_tokens = "”’\"')）】》」』"
    matches = re.findall(
        rf"[^{re.escape(end_tokens)}]+(?:[{re.escape(end_tokens)}]+[{re.escape(closing_tokens)}]*)?",
        normalized,
    )

    sentences: list[str] = []
    for raw in matches:
        item = str(raw or "").strip()
        if not item:
            continue

        if sentences:
            leading_closers = re.match(rf"^[{re.escape(closing_tokens)}]+", item)
            if leading_closers:
                closer_text = leading_closers.group(0)
                sentences[-1] = f"{sentences[-1]}{closer_text}"
                item = item[len(closer_text):].strip()
                if not item:
                    continue

        if sentences and not re.search(r"[\w一-鿿]", item):
            sentences[-1] = f"{sentences[-1]}{item}"
            continue

        sentences.append(item)

    if sentences:
        return sentences
    return [normalized]


def split_script_reading_chunks(
    script: str,
    *,
    max_chars: int = _DEFAULT_READING_MAX_CHARS,
    min_chars: int = _DEFAULT_READING_MIN_CHARS,
) -> list[str]:
    """Split script into readable subtitle/narration chunks.

    Hard sentence punctuation always closes a chunk. Soft punctuation is only a
    break opportunity, so short lead-ins can stay attached while long clauses do
    not become one glued subtitle line.
    """
    max_chars = max(6, int(max_chars or _DEFAULT_READING_MAX_CHARS))
    min_chars = max(1, min(int(min_chars or _DEFAULT_READING_MIN_CHARS), max_chars))
    punctuation = "".join(_HARD_ENDS + _SOFT_ENDS)
    closing_tokens = "”’\"')）】》」』"
    chunks: list[str] = []

    for sentence in split_script_sentences(script):
        clauses = re.findall(
            rf"[^{re.escape(punctuation)}]+(?:[{re.escape(punctuation)}]+[{re.escape(closing_tokens)}]*)?",
            sentence,
        )
        if not clauses:
            clauses = [sentence]
        current = ""
        for raw_clause in clauses:
            clause = str(raw_clause or "").strip()
            if not clause:
                continue
            if not current:
                current = clause
            elif len(clean_text_for_timing(current + clause)) <= max_chars:
                current = f"{current}{clause}"
            else:
                chunks.extend(_wrap_long_reading_chunk(current, max_chars=max_chars))
                current = clause

            clean_len = len(clean_text_for_timing(current))
            if is_hard_sentence_end(current) or (
                is_soft_sentence_end(current) and clean_len >= min_chars
            ):
                chunks.extend(_wrap_long_reading_chunk(current, max_chars=max_chars))
                current = ""
        if current:
            chunks.extend(_wrap_long_reading_chunk(current, max_chars=max_chars))

    return [chunk for chunk in chunks if chunk]


def _wrap_long_reading_chunk(text: str, *, max_chars: int) -> list[str]:
    item = str(text or "").strip()
    if not item:
        return []
    if len(clean_text_for_timing(item)) <= max_chars:
        return [item]

    chunks: list[str] = []
    current = ""
    clean_count = 0
    for char in item:
        current += char
        if re.match(r"[\w一-鿿]", char):
            clean_count += 1
        if clean_count >= max_chars:
            chunks.append(current.strip())
            current = ""
            clean_count = 0
    if current.strip():
        chunks.append(current.strip())
    return chunks
