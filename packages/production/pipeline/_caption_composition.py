"""Deterministic fixed-band caption composition with inline emphasis runs."""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from packages.core.contracts import SpeechTokenTiming
from packages.core.contracts.artifacts import (
    CaptionBand,
    CaptionCompositionDiagnostics,
    CaptionLayoutFallback,
    CaptionCompositionPlanArtifact,
    CaptionCue,
    CaptionFrameSpan,
    CaptionLine,
    CaptionRun,
    CaptionTokenFallback,
    CaptionUnitFallback,
    EmphasisHint,
    NarrationUnit,
)

_MIN_DISPLAY_SEC = 0.6
_MERGE_GAP_SEC = 0.3
_SHORT_CUE_MEANINGFUL = 3
_MAX_SPLIT_DEPTH = 8
_FORBID_LINE_START = set("，。！？；：、）》】」』｝〕…”’，,.!?;:%)]}\"'")
_FORBID_LINE_END = set("（《【「『｛〔“‘([{\"'")
_SENTENCE_END = set("。！？；!?;")
_PAUSE = set("，、：,:")
_TOKEN_PATTERNS = (
    re.compile(r"\d+(?:\.\d+)?(?:元起?|折|%|％)"),
    re.compile(r"\d+(?:\.\d+)?[xX倍]"),
    re.compile(r"\d+(?:\.\d+)?\s*[cmk]?m?[×xX]\s*\d+(?:\.\d+)?\s*[cmk]?m?"),
    re.compile(r"[A-Za-z0-9.\-]+"),
    re.compile(r"\d+月\d+日?"),
)


@dataclass
class _Cue:
    start: float
    end: float
    char_start: int
    char_end: int
    source_unit_ids: list[str]


@dataclass(frozen=True)
class _BoundHint:
    hint_id: str
    phrase: str
    priority: int
    display_mode: str
    start: int
    end: int
    order: int
    font_asset_id: str | None = None


@dataclass
class _LaidOutCue:
    cue: _Cue
    lines: list[tuple[int, int]]
    omitted: list[tuple[int, int]] = field(default_factory=list)


def build_caption_composition(
    *,
    script: str,
    units: list[NarrationUnit],
    tokens: list[SpeechTokenTiming],
    hints: list[EmphasisHint],
    fps: int,
    total_frames: int,
    width: int,
    height: int,
    band: CaptionBand,
    normal_enabled: bool,
    emphasis_enabled: bool,
    normal_font_asset_id: str | None,
    emphasis_font_asset_id: str | None,
    normal_font_size: int,
    emphasis_font_size: int,
    normal_measure: Callable[[str], float],
    emphasis_measure: Callable[[str], float],
    normal_baseline_offset: float,
    emphasis_baseline_offset: float,
    timing_source: str,
    normal_metrics_source: str,
    emphasis_metrics_source: str,
    emphasis_font_asset_ids: list[str | None] | None = None,
    emphasis_measures_by_asset: Mapping[str, Callable[[str], float]] | None = None,
    emphasis_baseline_offsets_by_asset: Mapping[str, float] | None = None,
    font_horizontal_overhang_px: Mapping[str, float] | None = None,
    font_horizontal_left_overhang_px: Mapping[str, float] | None = None,
    font_horizontal_right_overhang_px: Mapping[str, float] | None = None,
    layout_horizontal_overhang_px: float = 0.0,
) -> CaptionCompositionPlanArtifact:
    """Compile one authoritative caption artifact without inspecting video pixels."""

    diagnostics = CaptionCompositionDiagnostics(
        timing_source=timing_source,
        font_metrics_source=normal_metrics_source,
        emphasis_font_metrics_source=emphasis_metrics_source,
        hints_total=len(hints) if emphasis_enabled else 0,
        font_horizontal_overhang_px=dict(font_horizontal_overhang_px or {}),
        font_horizontal_left_overhang_px=dict(font_horizontal_left_overhang_px or {}),
        font_horizontal_right_overhang_px=dict(font_horizontal_right_overhang_px or {}),
    )
    if not normal_enabled:
        return CaptionCompositionPlanArtifact(
            fps=fps,
            width=width,
            height=height,
            normal_enabled=False,
            emphasis_enabled=False,
            band=band,
            normal_font_asset_id=None,
            emphasis_font_asset_id=None,
            normal_font_size=normal_font_size,
            emphasis_font_size=emphasis_font_size,
            cues=[],
            diagnostics=diagnostics,
        )

    cues = _merge_cues(_locate_units(script, units, diagnostics), script, diagnostics)
    enabled_hints = hints if emphasis_enabled else []
    if (
        emphasis_font_asset_ids is not None
        and len(emphasis_font_asset_ids) != len(enabled_hints)
    ):
        raise ValueError("emphasis_font_asset_ids must align with enabled emphasis hints")
    bound = _bind_hints(
        script,
        enabled_hints,
        diagnostics,
        font_asset_ids=emphasis_font_asset_ids,
    )
    timed_hints: list[_BoundHint] = []
    for hint in bound:
        phrase_end = hint.start + len(hint.phrase)
        if _tokens_cover_meaningful_span(script, tokens, hint.start, phrase_end):
            timed_hints.append(hint)
            continue
        diagnostics.hints_token_unmatched += 1
        diagnostics.fallbacks.append(
            CaptionTokenFallback(hint_ids=[hint.hint_id], phrase=hint.phrase)
        )
    by_cue = _resolve_hints_by_cue(cues, timed_hints, script, diagnostics)
    max_width = width * band.max_width_ratio - max(0.0, layout_horizontal_overhang_px)
    if max_width <= 0:
        raise ValueError("caption horizontal overhang leaves no usable layout width")
    laid_out: list[tuple[_LaidOutCue, list[_BoundHint]]] = []
    for cue_index, cue in enumerate(cues):
        cue_hints = by_cue.get(cue_index, [])
        values, failed = _layout_cue(
            cue,
            script=script,
            hints=cue_hints,
            tokens=tokens,
            normal_measure=normal_measure,
            emphasis_measure=emphasis_measure,
            emphasis_measures_by_asset=emphasis_measures_by_asset,
            max_width=max_width,
            diagnostics=diagnostics,
        )
        if failed:
            diagnostics.hints_unbreakable += len(cue_hints)
            diagnostics.fallbacks.append(
                CaptionLayoutFallback(
                    source_unit_ids=list(cue.source_unit_ids),
                    hint_ids=[item.hint_id for item in cue_hints],
                )
            )
            cue_hints = []
            values, _ = _layout_cue(
                cue,
                script=script,
                hints=[],
                tokens=tokens,
                normal_measure=normal_measure,
                emphasis_measure=emphasis_measure,
                emphasis_measures_by_asset=emphasis_measures_by_asset,
                max_width=max_width,
                diagnostics=diagnostics,
            )
        laid_out.extend((value, cue_hints) for value in values)

    caption_cues = [
        _materialize_cue(
            index=index,
            item=item,
            hints=item_hints,
            script=script,
            tokens=tokens,
            fps=fps,
            total_frames=total_frames,
            normal_measure=normal_measure,
            emphasis_measure=emphasis_measure,
            emphasis_measures_by_asset=emphasis_measures_by_asset,
            normal_baseline_offset=normal_baseline_offset,
            emphasis_baseline_offset=emphasis_baseline_offset,
            emphasis_baseline_offsets_by_asset=emphasis_baseline_offsets_by_asset,
        )
        for index, (item, item_hints) in enumerate(laid_out)
    ]
    applied_hint_ids = {
        run.hint_id
        for cue in caption_cues
        for line in cue.lines
        for run in line.runs
        if run.hint_id
    }
    diagnostics.hints_applied = len(applied_hint_ids)
    return CaptionCompositionPlanArtifact(
        fps=fps,
        width=width,
        height=height,
        normal_enabled=True,
        emphasis_enabled=bool(emphasis_enabled),
        band=band,
        normal_font_asset_id=normal_font_asset_id,
        emphasis_font_asset_id=emphasis_font_asset_id if emphasis_enabled else None,
        normal_font_size=normal_font_size,
        emphasis_font_size=emphasis_font_size,
        cues=caption_cues,
        diagnostics=diagnostics,
    )


def _locate_units(
    script: str,
    units: list[NarrationUnit],
    diagnostics: CaptionCompositionDiagnostics,
) -> list[_Cue]:
    result: list[_Cue] = []
    normalized_script, source_spans = _normalize_with_source_spans(script)
    cursor = 0
    for index, unit in enumerate(units):
        unit_id = unit.unit_id or f"unit_{index + 1:04d}"
        raw_text = unit.text
        text, _ = _normalize_with_source_spans(raw_text)
        if not text or not source_spans:
            diagnostics.units_unmatched += 1
            diagnostics.fallbacks.append(
                CaptionUnitFallback(
                    reason="narration_unit_unmatched",
                    source_unit_ids=[unit_id],
                    text=raw_text,
                )
            )
            continue
        normalized_start = normalized_script.find(text, cursor)
        if normalized_start < 0:
            diagnostics.units_unmatched += 1
            diagnostics.fallbacks.append(
                CaptionUnitFallback(
                    reason="narration_unit_unmatched",
                    source_unit_ids=[unit_id],
                    text=raw_text,
                )
            )
            continue
        normalized_end = normalized_start + len(text)
        start = source_spans[normalized_start][0]
        end = source_spans[normalized_end - 1][1]
        unit_start = max(0.0, unit.start)
        unit_end = max(unit_start, unit.end)
        if unit_end <= unit_start:
            diagnostics.units_unmatched += 1
            diagnostics.fallbacks.append(
                CaptionUnitFallback(
                    reason="narration_unit_timing_invalid",
                    source_unit_ids=[unit_id],
                    text=raw_text,
                )
            )
            continue
        result.append(
            _Cue(
                start=unit_start,
                end=unit_end,
                char_start=start,
                char_end=end,
                source_unit_ids=[unit_id],
            )
        )
        cursor = normalized_end
    return result


def _merge_cues(
    cues: list[_Cue], script: str, diagnostics: CaptionCompositionDiagnostics
) -> list[_Cue]:
    merged: list[_Cue] = []
    index = 0
    while index < len(cues):
        cue = cues[index]
        index += 1
        text = script[cue.char_start : cue.char_end]
        meaningful = sum(_meaningful(char) for char in text)
        while (
            not merged
            and index < len(cues)
            and meaningful <= _SHORT_CUE_MEANINGFUL
            and cues[index].start - cue.end < _MERGE_GAP_SEC
        ):
            following = cues[index]
            index += 1
            cue = _Cue(
                start=cue.start,
                end=following.end,
                char_start=cue.char_start,
                char_end=following.char_end,
                source_unit_ids=[*cue.source_unit_ids, *following.source_unit_ids],
            )
            text = script[cue.char_start : cue.char_end]
            meaningful = sum(_meaningful(char) for char in text)
            diagnostics.merged_units += 1
        if merged and (
            meaningful == 0
            or (
                meaningful <= _SHORT_CUE_MEANINGFUL
                and cue.start - merged[-1].end < _MERGE_GAP_SEC
            )
        ):
            previous = merged[-1]
            previous.end = cue.end
            previous.char_end = cue.char_end
            previous.source_unit_ids.extend(cue.source_unit_ids)
            diagnostics.merged_units += 1
            continue
        merged.append(cue)
    return merged


def _bind_hints(
    script: str,
    hints: list[EmphasisHint],
    diagnostics: CaptionCompositionDiagnostics,
    *,
    font_asset_ids: list[str | None] | None = None,
) -> list[_BoundHint]:
    occupied_by_phrase: dict[str, list[tuple[int, int]]] = {}
    result: list[_BoundHint] = []
    for order, hint in enumerate(hints):
        phrase = hint.phrase.strip()
        if not phrase:
            diagnostics.hints_unmatched += 1
            continue
        cursor = 0
        match: tuple[int, int] | None = None
        while True:
            start = script.find(phrase, cursor)
            if start < 0:
                break
            end = start + len(phrase)
            phrase_instances = occupied_by_phrase.get(phrase, [])
            if not any(
                start < used_end and used_start < end
                for used_start, used_end in phrase_instances
            ):
                match = (start, end)
                break
            cursor = start + 1
        if match is None:
            diagnostics.hints_unmatched += 1
            continue
        occupied_by_phrase.setdefault(phrase, []).append(match)
        result.append(
            _BoundHint(
                hint_id=f"hint_{order + 1:04d}",
                phrase=phrase,
                priority=hint.priority,
                display_mode=hint.display_mode,
                start=match[0],
                end=match[1],
                order=order,
                font_asset_id=font_asset_ids[order] if font_asset_ids is not None else None,
            )
        )
    return result


def _resolve_hints_by_cue(
    cues: list[_Cue],
    hints: list[_BoundHint],
    script: str,
    diagnostics: CaptionCompositionDiagnostics,
) -> dict[int, list[_BoundHint]]:
    result: dict[int, list[_BoundHint]] = {}
    assigned_hint_ids: set[str] = set()
    for cue_index, cue in enumerate(cues):
        exact_candidates = [
            item for item in hints if cue.char_start <= item.start and item.end <= cue.char_end
        ]
        candidates = [
            _BoundHint(
                hint_id=item.hint_id,
                phrase=item.phrase,
                priority=item.priority,
                display_mode=item.display_mode,
                start=item.start,
                end=_extend_run_end(script, item.end, limit=cue.char_end),
                order=item.order,
                font_asset_id=item.font_asset_id,
            )
            for item in exact_candidates
        ]
        assigned_hint_ids.update(item.hint_id for item in candidates)
        whole = [item for item in candidates if item.display_mode == "whole_cue"]
        if whole:
            chosen = sorted(whole, key=lambda item: (-item.priority, -len(item.phrase), item.order))[0]
            diagnostics.hints_overlapped += max(0, len(candidates) - 1)
            result[cue_index] = [
                _BoundHint(
                    hint_id=chosen.hint_id,
                    phrase=chosen.phrase,
                    priority=chosen.priority,
                    display_mode="whole_cue",
                    start=cue.char_start,
                    end=cue.char_end,
                    order=chosen.order,
                    font_asset_id=chosen.font_asset_id,
                )
            ]
            continue
        kept: list[_BoundHint] = []
        for item in sorted(
            candidates, key=lambda value: (-value.priority, -len(value.phrase), value.start, value.order)
        ):
            if any(item.start < other.end and other.start < item.end for other in kept):
                diagnostics.hints_overlapped += 1
                continue
            kept.append(item)
        result[cue_index] = sorted(kept, key=lambda item: item.start)
    diagnostics.hints_unmatched += len(
        [item for item in hints if item.hint_id not in assigned_hint_ids]
    )
    return result


def _layout_cue(
    cue: _Cue,
    *,
    script: str,
    hints: list[_BoundHint],
    tokens: list[SpeechTokenTiming],
    normal_measure: Callable[[str], float],
    emphasis_measure: Callable[[str], float],
    max_width: float,
    diagnostics: CaptionCompositionDiagnostics,
    emphasis_measures_by_asset: Mapping[str, Callable[[str], float]] | None = None,
    depth: int = 0,
) -> tuple[list[_LaidOutCue], bool]:
    lines = _choose_lines(
        cue,
        script=script,
        hints=hints,
        tokens=tokens,
        normal_measure=normal_measure,
        emphasis_measure=emphasis_measure,
        emphasis_measures_by_asset=emphasis_measures_by_asset,
        max_width=max_width,
    )
    if lines is not None:
        return [_LaidOutCue(cue=cue, lines=lines, omitted=_omitted_breaks(cue, lines, script))], False
    break_at = _time_split_index(cue, script=script, hints=hints, tokens=tokens)
    if break_at is None or depth >= _MAX_SPLIT_DEPTH:
        if hints:
            return [], True
        relaxed = _choose_lines(
            cue,
            script=script,
            hints=[],
            tokens=tokens,
            normal_measure=normal_measure,
            emphasis_measure=emphasis_measure,
            emphasis_measures_by_asset=emphasis_measures_by_asset,
            max_width=float("inf"),
        ) or [(cue.char_start, cue.char_end)]
        return [_LaidOutCue(cue=cue, lines=relaxed, omitted=_omitted_breaks(cue, relaxed, script))], False
    left, right = _split_cue(cue, break_at, script)
    diagnostics.split_cues += 1
    whole_hints = [item for item in hints if item.display_mode == "whole_cue"]
    left_hints = whole_hints or [item for item in hints if item.end <= left.char_end]
    right_hints = whole_hints or [item for item in hints if item.start >= right.char_start]
    left_values, left_failed = _layout_cue(
        left,
        script=script,
        hints=left_hints,
        tokens=tokens,
        normal_measure=normal_measure,
        emphasis_measure=emphasis_measure,
        emphasis_measures_by_asset=emphasis_measures_by_asset,
        max_width=max_width,
        diagnostics=diagnostics,
        depth=depth + 1,
    )
    right_values, right_failed = _layout_cue(
        right,
        script=script,
        hints=right_hints,
        tokens=tokens,
        normal_measure=normal_measure,
        emphasis_measure=emphasis_measure,
        emphasis_measures_by_asset=emphasis_measures_by_asset,
        max_width=max_width,
        diagnostics=diagnostics,
        depth=depth + 1,
    )
    return left_values + right_values, left_failed or right_failed


def _choose_lines(
    cue: _Cue,
    *,
    script: str,
    hints: list[_BoundHint],
    tokens: list[SpeechTokenTiming],
    normal_measure: Callable[[str], float],
    emphasis_measure: Callable[[str], float],
    emphasis_measures_by_asset: Mapping[str, Callable[[str], float]] | None,
    max_width: float,
) -> list[tuple[int, int]] | None:
    protected = _protected_spans(cue, script, hints, tokens)
    legal = [
        index
        for index in range(cue.char_start + 1, cue.char_end)
        if _legal_break(script, cue, index, protected)
    ]
    for line_count in range(1, 4):
        best: tuple[tuple[float, tuple[int, ...]], list[tuple[int, int]]] | None = None
        combos = [()] if line_count == 1 else itertools.combinations(legal, line_count - 1)
        for breaks in combos:
            bounds = (cue.char_start, *breaks, cue.char_end)
            spans = [_trim_span(script, bounds[i], bounds[i + 1]) for i in range(line_count)]
            if any(start >= end for start, end in spans):
                continue
            widths = [
                _mixed_width(
                    start,
                    end,
                    script=script,
                    hints=hints,
                    normal_measure=normal_measure,
                    emphasis_measure=emphasis_measure,
                    emphasis_measures_by_asset=emphasis_measures_by_asset,
                )
                for start, end in spans
            ]
            if any(width > max_width + 1e-6 for width in widths):
                continue
            if line_count == 1:
                return spans
            average = sum(widths) / len(widths)
            imbalance = sum(abs(value - average) for value in widths) / max(sum(widths), 1.0)
            punctuation = sum(
                _break_penalty(script[spans[index][1] - 1]) for index in range(len(spans) - 1)
            )
            key = (3.0 * imbalance + punctuation, tuple(breaks))
            if best is None or key < best[0]:
                best = (key, spans)
        if best is not None:
            return best[1]
    return None


def _protected_spans(
    cue: _Cue,
    script: str,
    hints: list[_BoundHint],
    tokens: list[SpeechTokenTiming],
) -> list[tuple[int, int]]:
    text = script[cue.char_start : cue.char_end]
    spans = [
        (cue.char_start + match.start(), cue.char_start + match.end())
        for pattern in _TOKEN_PATTERNS
        for match in pattern.finditer(text)
    ]
    spans.extend((item.start, item.end) for item in hints if item.display_mode == "inline")
    spans.extend(
        token.char_span
        for token in tokens
        if token.char_span is not None
        and cue.char_start <= token.char_span[0]
        and token.char_span[1] <= cue.char_end
    )
    return spans


def _legal_break(script: str, cue: _Cue, index: int, protected: list[tuple[int, int]]) -> bool:
    if any(start < index < end for start, end in protected):
        return False
    left_start, left_end = _trim_span(script, cue.char_start, index)
    right_start, right_end = _trim_span(script, index, cue.char_end)
    if left_start >= left_end or right_start >= right_end:
        return False
    if script[left_end - 1] in _FORBID_LINE_END or script[right_start] in _FORBID_LINE_START:
        return False
    return sum(_meaningful(char) for char in script[right_start:right_end]) >= 2


def _time_split_index(
    cue: _Cue,
    *,
    script: str,
    hints: list[_BoundHint],
    tokens: list[SpeechTokenTiming],
) -> int | None:
    protected = _protected_spans(cue, script, hints, tokens)
    candidates = [
        index
        for index in range(cue.char_start + 1, cue.char_end)
        if _legal_break(script, cue, index, protected)
    ]
    if not candidates:
        return None
    midpoint = (cue.char_start + cue.char_end) / 2.0
    return min(
        candidates,
        key=lambda index: (
            0 if script[index - 1] in _SENTENCE_END else 1 if script[index - 1] in _PAUSE else 2,
            abs(index - midpoint),
            index,
        ),
    )


def _split_cue(cue: _Cue, index: int, script: str) -> tuple[_Cue, _Cue]:
    total = cue.end - cue.start
    left_chars = sum(not char.isspace() for char in script[cue.char_start:index])
    all_chars = sum(not char.isspace() for char in script[cue.char_start : cue.char_end]) or 1
    left_duration = total * left_chars / all_chars
    if total >= 2 * _MIN_DISPLAY_SEC:
        left_duration = min(max(left_duration, _MIN_DISPLAY_SEC), total - _MIN_DISPLAY_SEC)
    split_time = cue.start + left_duration
    left_start, left_end = _trim_span(script, cue.char_start, index)
    right_start, right_end = _trim_span(script, index, cue.char_end)
    return (
        _Cue(cue.start, split_time, left_start, left_end, list(cue.source_unit_ids)),
        _Cue(split_time, cue.end, right_start, right_end, list(cue.source_unit_ids)),
    )


def _materialize_cue(
    *,
    index: int,
    item: _LaidOutCue,
    hints: list[_BoundHint],
    script: str,
    tokens: list[SpeechTokenTiming],
    fps: int,
    total_frames: int,
    normal_measure: Callable[[str], float],
    emphasis_measure: Callable[[str], float],
    emphasis_measures_by_asset: Mapping[str, Callable[[str], float]] | None,
    normal_baseline_offset: float,
    emphasis_baseline_offset: float,
    emphasis_baseline_offsets_by_asset: Mapping[str, float] | None,
) -> CaptionCue:
    cue = item.cue
    start_frame = min(total_frames, max(0, round(cue.start * fps)))
    end_frame = min(total_frames, max(start_frame + 1, round(cue.end * fps)))
    cue_tokens = [token for token in tokens if _token_inside(token, cue.char_start, cue.char_end)]
    whole = next((hint for hint in hints if hint.display_mode == "whole_cue"), None)
    lines: list[CaptionLine] = []
    run_counter = 0
    for line_start, line_end in item.lines:
        line_tokens = [token for token in cue_tokens if _token_inside(token, line_start, line_end)]
        line_enter = _line_start_frame(
            script,
            line_start,
            line_end,
            line_tokens,
            fps,
            start_frame,
            end_frame,
        )
        segments: list[tuple[int, int, _BoundHint | None]] = []
        if whole is not None:
            segments.append((line_start, line_end, whole))
        else:
            cursor = line_start
            for hint in [hint for hint in hints if line_start <= hint.start and hint.end <= line_end]:
                if cursor < hint.start:
                    segments.append((cursor, hint.start, None))
                segments.append((hint.start, hint.end, hint))
                cursor = hint.end
            if cursor < line_end:
                segments.append((cursor, line_end, None))
        runs: list[CaptionRun] = []
        for segment_start, segment_end, hint in segments:
            if segment_start >= segment_end:
                continue
            run_counter += 1
            role = "emphasis" if hint is not None else "normal"
            run_tokens = [
                token for token in line_tokens if _token_inside(token, segment_start, segment_end)
            ]
            enter_frame = (
                _token_start_frame(run_tokens, fps, line_enter, end_frame)
                if role == "emphasis"
                else line_enter
            )
            text = script[segment_start:segment_end]
            measure = (
                _emphasis_measure(hint, emphasis_measure, emphasis_measures_by_asset)
                if role == "emphasis"
                else normal_measure
            )
            emphasis_baseline = (
                emphasis_baseline_offsets_by_asset.get(
                    hint.font_asset_id, emphasis_baseline_offset
                )
                if hint is not None
                and hint.font_asset_id
                and emphasis_baseline_offsets_by_asset is not None
                else emphasis_baseline_offset
            )
            runs.append(
                CaptionRun(
                    run_id=f"cue_{index + 1:04d}_run_{run_counter:03d}",
                    text=text,
                    role=role,
                    hint_id=hint.hint_id if hint else None,
                    font_asset_id=hint.font_asset_id if hint else None,
                    token_ids=[token.token_id for token in run_tokens if token.token_id],
                    char_span=(segment_start - cue.char_start, segment_end - cue.char_start),
                    enter_frame=max(start_frame, min(end_frame - 1, enter_frame)),
                    exit_frame=end_frame,
                    effect_id="pop" if role == "emphasis" else "soft_in",
                    advance_px=round(measure(text), 3),
                    baseline_offset_px=round(
                        emphasis_baseline if role == "emphasis" else normal_baseline_offset,
                        3,
                    ),
                )
            )
        lines.append(
            CaptionLine(runs=runs, advance_px=round(sum(run.advance_px for run in runs), 3))
        )
    cue_text = script[cue.char_start : cue.char_end]
    return CaptionCue(
        cue_id=f"cue_{index + 1:04d}",
        text=cue_text,
        start_frame=start_frame,
        end_frame=end_frame,
        spoken_span=CaptionFrameSpan(start_frame=start_frame, end_frame=end_frame),
        display_span=CaptionFrameSpan(start_frame=start_frame, end_frame=end_frame),
        source_unit_ids=list(cue.source_unit_ids),
        lines=lines,
        omitted_break_whitespace=[
            (start - cue.char_start, end - cue.char_start) for start, end in item.omitted
        ],
    )


def _mixed_width(
    start: int,
    end: int,
    *,
    script: str,
    hints: list[_BoundHint],
    normal_measure: Callable[[str], float],
    emphasis_measure: Callable[[str], float],
    emphasis_measures_by_asset: Mapping[str, Callable[[str], float]] | None,
) -> float:
    whole = next((item for item in hints if item.display_mode == "whole_cue"), None)
    if whole is not None:
        return _emphasis_measure(whole, emphasis_measure, emphasis_measures_by_asset)(
            script[start:end]
        )
    cursor = start
    width = 0.0
    for hint in [item for item in hints if start <= item.start and item.end <= end]:
        width += normal_measure(script[cursor : hint.start])
        width += _emphasis_measure(hint, emphasis_measure, emphasis_measures_by_asset)(
            script[hint.start : hint.end]
        )
        cursor = hint.end
    return width + normal_measure(script[cursor:end])


def _emphasis_measure(
    hint: _BoundHint | None,
    default: Callable[[str], float],
    by_asset: Mapping[str, Callable[[str], float]] | None,
) -> Callable[[str], float]:
    if hint is not None and hint.font_asset_id and by_asset is not None:
        return by_asset.get(hint.font_asset_id, default)
    return default


def _token_inside(token: SpeechTokenTiming, start: int, end: int) -> bool:
    return token.char_span is not None and start <= token.char_span[0] and token.char_span[1] <= end


def _tokens_cover_meaningful_span(
    script: str,
    tokens: list[SpeechTokenTiming],
    start: int,
    end: int,
) -> bool:
    required = {index for index in range(start, end) if _meaningful(script[index])}
    if not required:
        return False
    covered: set[int] = set()
    for token in tokens:
        span = token.char_span
        if span is None:
            continue
        token_start, token_end = span
        if token_start < start or token_end > end or token_end <= token_start:
            continue
        covered.update(range(token_start, token_end))
    return required.issubset(covered)


def _line_start_frame(
    script: str,
    start: int,
    end: int,
    tokens: list[SpeechTokenTiming],
    fps: int,
    fallback: int,
    end_frame: int,
) -> int:
    first_meaningful = next(
        (index for index in range(start, end) if _meaningful(script[index])),
        None,
    )
    if first_meaningful is None:
        return fallback
    owner = [
        token
        for token in tokens
        if token.char_span is not None
        and token.char_span[0] <= first_meaningful < token.char_span[1]
    ]
    return _token_start_frame(owner, fps, fallback, end_frame)


def _token_start_frame(
    tokens: list[SpeechTokenTiming], fps: int, fallback: int, end_frame: int
) -> int:
    if not tokens:
        return fallback
    start = min(token.start for token in tokens)
    return max(fallback, min(end_frame - 1, round(start * fps)))


def _omitted_breaks(
    cue: _Cue, lines: list[tuple[int, int]], script: str
) -> list[tuple[int, int]]:
    omitted: list[tuple[int, int]] = []
    cursor = cue.char_start
    for start, end in lines:
        if cursor < start and script[cursor:start]:
            omitted.append((cursor, start))
        cursor = end
    if cursor < cue.char_end and script[cursor : cue.char_end]:
        omitted.append((cursor, cue.char_end))
    return omitted


def _trim_span(script: str, start: int, end: int) -> tuple[int, int]:
    while start < end and script[start].isspace():
        start += 1
    while end > start and script[end - 1].isspace():
        end -= 1
    return start, end


def _normalize_with_source_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Collapse whitespace while retaining exact half-open offsets in ``text``."""

    normalized: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if not text[index].isspace():
            normalized.append(text[index])
            spans.append((index, index + 1))
            index += 1
            continue
        whitespace_start = index
        while index < len(text) and text[index].isspace():
            index += 1
        if normalized and index < len(text):
            normalized.append(" ")
            spans.append((whitespace_start, index))
    return "".join(normalized), spans


def _extend_run_end(script: str, end: int, *, limit: int) -> int:
    """Assign boundary whitespace/punctuation to the preceding inline run."""

    while end < limit:
        char = script[end]
        if not char.isspace() and unicodedata.category(char)[0] != "P":
            break
        end += 1
    return end


def _meaningful(char: str) -> int:
    return int(not char.isspace() and unicodedata.category(char)[0] != "P")


def _break_penalty(char: str) -> float:
    if char in _SENTENCE_END:
        return 0.0
    if char in _PAUSE:
        return 0.2
    return 1.0
