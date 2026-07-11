"""Provider-neutral speech timing normalization and text-basis repair."""

from __future__ import annotations

import re
import unicodedata

from packages.core.contracts import (
    SpeechSegmentTiming,
    SpeechTiming,
    SpeechTokenTiming,
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])")
_DISPLAY_TOKEN = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*|\d+(?:\.\d+)?|[^\s]")


def normalize_timing_for_script(
    timing: SpeechTiming,
    *,
    script: str,
    duration: float,
) -> tuple[list[SpeechSegmentTiming], list[SpeechTokenTiming], dict[str, int]]:
    """Return valid timing aligned to display text.

    Native tokens are retained when their normalized text agrees with the script.
    Text-normalization mismatches (for example ``2000`` spoken as ``两千``) fall
    back inside each sentence/segment to deterministic display-token proportions.
    """

    limit = max(0.001, float(duration or 0.001))
    segments = _valid_segments(timing.segments, limit)
    tokens = _valid_tokens(timing.tokens, limit)
    diagnostics = {"token_matched": 0, "char_fallback": 0, "invalid_dropped": 0}
    diagnostics["invalid_dropped"] = (
        len(timing.segments) - len(segments) + len(timing.tokens) - len(tokens)
    )

    if not segments and tokens:
        segments = [
            SpeechSegmentTiming(text=script, start=tokens[0].start, end=tokens[-1].end)
        ]
    if not segments:
        return [], [], diagnostics

    script_normalized = normalize_speech_text(script)
    token_normalized = normalize_speech_text("".join(item.text for item in tokens))
    if tokens and token_normalized and token_normalized in script_normalized:
        diagnostics["token_matched"] = len(tokens)
        return segments, tokens, diagnostics

    repaired_segments = _segments_with_display_text(segments, script)
    repaired_tokens: list[SpeechTokenTiming] = []
    for segment in repaired_segments:
        repaired_tokens.extend(
            proportional_tokens(segment.text, start=segment.start, end=segment.end)
        )
    diagnostics["char_fallback"] = len(repaired_tokens)
    return repaired_segments, repaired_tokens, diagnostics


def estimated_timing_for_script(script: str, *, duration: float) -> SpeechTiming:
    segments = _segments_with_display_text(
        [SpeechSegmentTiming(text=script, start=0.0, end=max(0.001, duration))],
        script,
    )
    tokens = [
        token
        for segment in segments
        for token in proportional_tokens(segment.text, start=segment.start, end=segment.end)
    ]
    return SpeechTiming(
        segments=segments,
        tokens=tokens,
        granularity="character",
        text_basis="original",
    )


def proportional_tokens(text: str, *, start: float, end: float) -> list[SpeechTokenTiming]:
    values = [match.group(0) for match in _DISPLAY_TOKEN.finditer(str(text or ""))]
    if not values or end <= start:
        return []
    weights = [max(1, len(normalize_speech_text(value))) for value in values]
    total = sum(weights)
    cursor = float(start)
    result: list[SpeechTokenTiming] = []
    for index, (value, weight) in enumerate(zip(values, weights, strict=True)):
        token_end = float(end) if index == len(values) - 1 else cursor + (end - start) * weight / total
        if token_end > cursor:
            result.append(SpeechTokenTiming(text=value, start=cursor, end=token_end))
        cursor = token_end
    return result


def normalize_speech_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char.lower() for char in normalized if char.isalnum())


def _valid_segments(
    values: list[SpeechSegmentTiming], duration: float
) -> list[SpeechSegmentTiming]:
    result = []
    for item in values:
        start = max(0.0, min(duration, item.start))
        end = max(0.0, min(duration, item.end))
        if item.text.strip() and end > start:
            result.append(item.model_copy(update={"start": start, "end": end}))
    return sorted(result, key=lambda item: (item.start, item.end, item.text))


def _valid_tokens(values: list[SpeechTokenTiming], duration: float) -> list[SpeechTokenTiming]:
    result = []
    for item in values:
        start = max(0.0, min(duration, item.start))
        end = max(0.0, min(duration, item.end))
        if item.text.strip() and end > start:
            result.append(item.model_copy(update={"start": start, "end": end}))
    return sorted(result, key=lambda item: (item.start, item.end, item.text))


def _segments_with_display_text(
    segments: list[SpeechSegmentTiming], script: str
) -> list[SpeechSegmentTiming]:
    sentences = [part.strip() for part in _SENTENCE_BOUNDARY.split(script) if part.strip()]
    if not sentences:
        sentences = [str(script or "").strip()]
    if len(sentences) == len(segments):
        return [
            segment.model_copy(update={"text": sentence})
            for segment, sentence in zip(segments, sentences, strict=True)
        ]
    if len(segments) == 1:
        return [segments[0].model_copy(update={"text": str(script or "").strip()})]

    # Different segmentation: split the display script proportionally across the
    # provider segments while preserving provider time boundaries.
    display_tokens = [match.group(0) for match in _DISPLAY_TOKEN.finditer(script)]
    if not display_tokens:
        return segments
    weights = [max(1, len(normalize_speech_text(item.text))) for item in segments]
    total_weight = sum(weights)
    cursor = 0
    repaired: list[SpeechSegmentTiming] = []
    accumulated = 0
    for index, (segment, weight) in enumerate(zip(segments, weights, strict=True)):
        accumulated += weight
        target = (
            len(display_tokens)
            if index == len(segments) - 1
            else max(cursor + 1, round(len(display_tokens) * accumulated / total_weight))
        )
        text = "".join(display_tokens[cursor:target])
        repaired.append(segment.model_copy(update={"text": text or segment.text}))
        cursor = target
    return repaired
