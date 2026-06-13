"""Pure narration / sentence text predicates (ported from editing_agent/narration_text.py).

No state. Used by the narration splitter to classify sentence ends and detect intent.
Behaviour is byte-identical to the origin's static helpers.
"""

from __future__ import annotations

import re

_HARD_ENDS = ("。", "！", "？", ".", "!", "?", "；", ";", "…")
_SOFT_ENDS = ("，", "、", "：", ",", ":")


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
