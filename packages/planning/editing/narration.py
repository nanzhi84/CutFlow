"""Narration-unit construction: split narration into boundary-eligible units.

Ported from editing_agent/narration_splitter.py. Produces :class:`NarrationUnit`
(the contract model, now carrying boundary fields) from one of three sources:
script sentences aligned to spoken segments, spoken segments directly, or script
text alone. ``portrait_cut_allowed`` / ``hard_end`` / ``boundary_score`` /
``pause_after_ms`` mark which unit ends are eligible portrait-cut boundaries — the
boundary planner consumes exactly these.

Input spoken segments are passed as :class:`SpokenSegment` (start/end/text) so the
splitter stays decoupled from any specific alignment artifact shape; the pipeline
node maps AlignmentSegment -> SpokenSegment.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.editing import _util as util
from packages.planning.editing import text as narration_text


@dataclass(frozen=True)
class SpokenSegment:
    """A timed spoken segment (ASR / forced-alignment), the splitter's input shape."""

    start: float
    end: float
    text: str


def _time_at_clean_char_boundary(
    *,
    target_clean_chars: float,
    asr_segments: Sequence[SpokenSegment],
    asr_clean_lengths: Sequence[int],
    total_asr_clean_chars: int,
    fallback_start: float,
    fallback_end: float,
) -> float:
    if not asr_segments or total_asr_clean_chars <= 0:
        return fallback_end
    target = util.clamp(float(target_clean_chars), 0.0, float(total_asr_clean_chars))
    consumed = 0.0
    for index, seg in enumerate(asr_segments):
        length = max(1.0, float(asr_clean_lengths[index] if index < len(asr_clean_lengths) else 1))
        next_consumed = consumed + length
        if target <= next_consumed or index == len(asr_segments) - 1:
            ratio = util.clamp((target - consumed) / length, 0.0, 1.0)
            seg_start = float(seg.start)
            seg_end = max(seg_start, float(seg.end))
            return util.round_time(seg_start + (seg_end - seg_start) * ratio)
        consumed = next_consumed
    return util.round_time(fallback_end if fallback_end > fallback_start else fallback_start)


def _pause_after_boundary_ms(
    *,
    boundary: float,
    asr_segments: Sequence[SpokenSegment],
    tolerance_sec: float = 0.12,
) -> int:
    for index, segment in enumerate(asr_segments[:-1]):
        seg_end = float(segment.end)
        next_start = float(asr_segments[index + 1].start)
        pause_sec = max(0.0, next_start - seg_end)
        if pause_sec <= 0:
            continue
        if abs(float(boundary) - seg_end) <= tolerance_sec:
            return int(round(pause_sec * 1000))
    return 0


def build_narration_units_from_script_sentences(
    *,
    script: str,
    asr_segments: Sequence[SpokenSegment],
    video_duration: float,
) -> list[NarrationUnit]:
    sentences = narration_text.split_script_reading_chunks(script)
    if len(sentences) <= 1 or not asr_segments:
        return []

    ordered = sorted(list(asr_segments or []), key=lambda seg: float(seg.start))
    if not ordered:
        return []

    script_lengths = [len(narration_text.clean_text_for_timing(sentence)) for sentence in sentences]
    total_script_chars = sum(script_lengths)
    if total_script_chars <= 0:
        return []

    asr_lengths = [len(narration_text.clean_text_for_timing(seg.text)) for seg in ordered]
    total_asr_chars = sum(asr_lengths)
    if total_asr_chars <= 0:
        return []

    timeline_start = util.round_time(ordered[0].start)
    timeline_end = util.round_time(max(float(ordered[-1].end), timeline_start))
    if timeline_end <= timeline_start:
        timeline_end = util.round_time(max(video_duration, timeline_start))

    boundaries = [timeline_start]
    cumulative_script_chars = 0
    for length in script_lengths[:-1]:
        cumulative_script_chars += length
        target_asr_chars = total_asr_chars * (cumulative_script_chars / total_script_chars)
        boundary = _time_at_clean_char_boundary(
            target_clean_chars=target_asr_chars,
            asr_segments=ordered,
            asr_clean_lengths=asr_lengths,
            total_asr_clean_chars=total_asr_chars,
            fallback_start=timeline_start,
            fallback_end=timeline_end,
        )
        if boundary <= boundaries[-1] + 0.08:
            duration_ratio = cumulative_script_chars / total_script_chars
            boundary = util.round_time(timeline_start + (timeline_end - timeline_start) * duration_ratio)
        boundaries.append(util.round_time(util.clamp(boundary, timeline_start, timeline_end)))
    boundaries.append(timeline_end)

    units: list[NarrationUnit] = []
    for idx, sentence in enumerate(sentences):
        start = util.round_time(boundaries[idx])
        end = util.round_time(max(start, boundaries[idx + 1]))
        if end - start <= 0.08:
            continue
        pause_after_ms = (
            0
            if idx == len(sentences) - 1
            else _pause_after_boundary_ms(
                boundary=end,
                asr_segments=ordered,
            )
        )
        hard_end = idx == len(sentences) - 1 or narration_text.is_hard_sentence_end(sentence)
        boundary_score = 0.35
        if narration_text.is_hard_sentence_end(sentence):
            boundary_score += 0.38
        boundary_score += min(max(pause_after_ms, 0) / 800.0, 0.25)
        if len(narration_text.clean_text_for_timing(sentence)) >= 10:
            boundary_score += 0.08
        boundary_score = round(util.clamp(boundary_score, 0.0, 1.0), 3)
        units.append(
            NarrationUnit(
                unit_id=f"unit_{idx + 1:03d}",
                text=sentence,
                start=start,
                end=end,
                confidence=1.0,
                duration=util.round_time(end - start),
                intent=narration_text.detect_narration_intent(sentence),
                pause_after_ms=pause_after_ms,
                hard_end=bool(hard_end),
                boundary_score=boundary_score,
                portrait_cut_allowed=bool(hard_end),
                boundary_reason="脚本句尾" if hard_end else "脚本阅读分句",
            )
        )
    return units


def allow_soft_portrait_boundary(*, text: str, pause_after_ms: int, duration: float) -> bool:
    if pause_after_ms >= 120 and duration >= 1.2:
        return True
    if narration_text.is_soft_sentence_end(text) and pause_after_ms > 0:
        return True
    return False


def build_narration_units_from_asr(
    asr_segments: Sequence[SpokenSegment],
    video_duration: float,
) -> list[NarrationUnit]:
    units: list[NarrationUnit] = []
    ordered = list(asr_segments or [])
    for idx, seg in enumerate(ordered):
        next_start = float(ordered[idx + 1].start) if idx + 1 < len(ordered) else max(float(seg.end), video_duration)
        pause_after_ms = int(round(max(0.0, next_start - float(seg.end)) * 1000))
        hard_end = (
            idx == len(ordered) - 1
            or narration_text.is_hard_sentence_end(seg.text)
            or pause_after_ms >= 240
        )
        soft_end = allow_soft_portrait_boundary(
            text=seg.text,
            pause_after_ms=pause_after_ms,
            duration=max(0.0, float(seg.end) - float(seg.start)),
        )
        boundary_score = 0.25
        if narration_text.is_hard_sentence_end(seg.text):
            boundary_score += 0.35
        elif narration_text.is_soft_sentence_end(seg.text):
            boundary_score += 0.2
        boundary_score += min(max(pause_after_ms, 0) / 800.0, 0.35)
        if len(str(seg.text or "").strip()) >= 10:
            boundary_score += 0.08
        boundary_score = round(util.clamp(boundary_score, 0.0, 1.0), 3)
        portrait_cut_allowed = bool(hard_end or soft_end)
        if idx == len(ordered) - 1:
            boundary_reason = "句尾收口"
        elif narration_text.is_hard_sentence_end(seg.text):
            boundary_reason = "标点收束"
        elif narration_text.is_soft_sentence_end(seg.text) and pause_after_ms > 0:
            boundary_reason = f"逗号停顿 {pause_after_ms}ms"
        elif pause_after_ms >= 240:
            boundary_reason = f"停顿 {pause_after_ms}ms"
        elif pause_after_ms >= 120:
            boundary_reason = f"短停顿 {pause_after_ms}ms"
        else:
            boundary_reason = "句中连续表达"
        units.append(
            NarrationUnit(
                unit_id=f"unit_{idx + 1:03d}",
                text=str(seg.text or "").strip(),
                start=util.round_time(seg.start),
                end=util.round_time(seg.end),
                confidence=1.0,
                duration=util.round_time(max(0.0, float(seg.end) - float(seg.start))),
                intent=narration_text.detect_narration_intent(seg.text),
                pause_after_ms=pause_after_ms,
                hard_end=bool(hard_end),
                boundary_score=boundary_score,
                portrait_cut_allowed=portrait_cut_allowed,
                boundary_reason=boundary_reason,
            )
        )
    return units


def build_narration_units_without_asr(
    script: str,
    video_duration: float,
) -> list[NarrationUnit]:
    sentences = narration_text.split_script_reading_chunks(script)
    if not sentences:
        return []
    total_chars = sum(max(1, len(re.sub(r"\s+", "", sentence))) for sentence in sentences)
    cursor = 0.0
    units: list[NarrationUnit] = []
    for idx, sentence in enumerate(sentences):
        char_weight = max(1, len(re.sub(r"\s+", "", sentence)))
        duration = max(0.8, video_duration * (char_weight / max(total_chars, 1)))
        end = video_duration if idx == len(sentences) - 1 else min(video_duration, cursor + duration)
        units.append(
            NarrationUnit(
                unit_id=f"unit_{idx + 1:03d}",
                text=sentence,
                start=util.round_time(cursor),
                end=util.round_time(end),
                confidence=1.0,
                duration=util.round_time(max(0.0, end - cursor)),
                intent=narration_text.detect_narration_intent(sentence),
                pause_after_ms=300 if idx < len(sentences) - 1 else 0,
                hard_end=True,
                boundary_score=0.78,
                portrait_cut_allowed=True,
                boundary_reason="句子切分回退",
            )
        )
        cursor = end
    return units


def should_prefer_asr_narration_units(
    script_units: Sequence[NarrationUnit],
    asr_units: Sequence[NarrationUnit],
) -> bool:
    if not script_units or not asr_units:
        return False

    def _is_suspicious(unit: NarrationUnit) -> bool:
        clean_len = len(narration_text.clean_text_for_timing(unit.text))
        has_meaningful_content = bool(re.search(r"[\w一-鿿]", str(unit.text or "")))
        return (
            not has_meaningful_content
            or clean_len <= 1
            or (util.unit_duration(unit) <= 1.2 and clean_len <= 2)
        )

    script_suspicious = [unit for unit in script_units if _is_suspicious(unit)]
    if not script_suspicious:
        script_avg_duration = sum(util.unit_duration(u) for u in script_units) / max(len(script_units), 1)
        asr_avg_duration = sum(util.unit_duration(u) for u in asr_units) / max(len(asr_units), 1)
        asr_soft_boundary_count = sum(
            1
            for unit in asr_units
            if unit.portrait_cut_allowed
            and (narration_text.is_soft_sentence_end(unit.text) or unit.pause_after_ms >= 120)
            and not unit.hard_end
        )
        asr_short_fragment_count = sum(1 for unit in asr_units if util.unit_duration(unit) < 1.0)
        if (
            len(asr_units) > len(script_units)
            and asr_soft_boundary_count > 0
            and asr_avg_duration <= max(6.5, script_avg_duration - 1.0)
            and asr_short_fragment_count <= max(1, len(asr_units) // 4)
        ):
            return True
        return False

    asr_suspicious = [unit for unit in asr_units if _is_suspicious(unit)]
    return len(asr_suspicious) < len(script_suspicious)


def build_narration_units(
    *,
    script: str,
    asr_segments: Sequence[SpokenSegment] | None,
    video_duration: float,
) -> list[NarrationUnit]:
    """Pick the best narration-unit source (mirrors the origin's coordinator).

    Prefer script-sentence units aligned to spoken segments; fall back to spoken
    segments directly when the script source produces suspicious fragments; fall
    back to script-only sentence splitting when there are no spoken segments.
    """
    asr = list(asr_segments or [])
    if asr:
        script_units = build_narration_units_from_script_sentences(
            script=script, asr_segments=asr, video_duration=video_duration
        )
        asr_units = build_narration_units_from_asr(asr, video_duration)
        if script_units and asr_units:
            return asr_units if should_prefer_asr_narration_units(script_units, asr_units) else script_units
        if script_units:
            return script_units
        if asr_units:
            return asr_units
    return build_narration_units_without_asr(script, video_duration)
