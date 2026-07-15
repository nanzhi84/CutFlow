"""Provider-neutral speech timing normalization and script-anchored token repair."""

from __future__ import annotations

import re
import unicodedata

from packages.core.contracts import (
    SpeechSegmentTiming,
    SpeechTiming,
    SpeechTokenTiming,
)

_DISPLAY_TOKEN = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*|\d+(?:\.\d+)?|[^\s]")
_ANCHOR_WINDOW = 10


def normalize_timing_for_script(
    timing: SpeechTiming,
    *,
    script: str,
    duration: float,
) -> tuple[list[SpeechSegmentTiming], list[SpeechTokenTiming], dict[str, int]]:
    """Return provider-raw segments and script-display tokens aligned to real time.

    Segments keep the provider's own text and timing (validated + ordered); the
    downstream narration builder proportionally maps script sentences onto them,
    which stays robust even when ASR re-segments the utterance. Tokens instead
    cover the script's *display* text (``_DISPLAY_TOKEN`` split): each display
    token matched to a provider token in sequence takes its real time (an
    anchor); unmatched tokens are interpolated between the surrounding anchors by
    character weight so number/typo drift never mis-times the caption.
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
            SpeechSegmentTiming(
                text="".join(item.text for item in tokens),
                start=tokens[0].start,
                end=tokens[-1].end,
            )
        ]
    if not segments:
        return [], [], diagnostics

    display_tokens, matched, fallback = _anchor_display_tokens(script, tokens, limit)
    diagnostics["token_matched"] = matched
    diagnostics["char_fallback"] = fallback
    return segments, display_tokens, diagnostics


def estimated_timing_for_script(script: str, *, duration: float) -> SpeechTiming:
    limit = max(0.001, float(duration))
    return SpeechTiming(
        segments=[SpeechSegmentTiming(text=str(script or "").strip(), start=0.0, end=limit)],
        tokens=proportional_tokens(script, start=0.0, end=limit),
        granularity="character",
        text_basis="original",
    )


def proportional_tokens(text: str, *, start: float, end: float) -> list[SpeechTokenTiming]:
    matches = list(_DISPLAY_TOKEN.finditer(str(text or "")))
    values = [match.group(0) for match in matches]
    if not values or end <= start:
        return []
    weights = [max(1, len(normalize_speech_text(value))) for value in values]
    total = sum(weights)
    cursor = float(start)
    result: list[SpeechTokenTiming] = []
    for index, (match, value, weight) in enumerate(zip(matches, values, weights, strict=True)):
        token_end = (
            float(end) if index == len(values) - 1 else cursor + (end - start) * weight / total
        )
        if token_end > cursor:
            result.append(
                SpeechTokenTiming(
                    text=value,
                    start=cursor,
                    end=token_end,
                    token_id=f"token_{index + 1:04d}",
                    char_span=(match.start(), match.end()),
                )
            )
        cursor = token_end
    return result


def assign_token_ownership(
    tokens: list[SpeechTokenTiming],
    *,
    script: str,
    units: list[object],
) -> list[SpeechTokenTiming]:
    """Attach stable script/token ownership without consulting token timestamps.

    The normalized token stream is already in script order.  This pass gives every
    token a deterministic identity and character range, then walks narration-unit
    text in the same order and claims each token exactly once.  A unit that cannot
    be matched leaves its tokens unowned instead of guessing from temporal overlap;
    CaptionCompositionPlanning records an explicit cue-level fallback for that case.
    """

    matches = list(_DISPLAY_TOKEN.finditer(str(script or "")))
    enriched: list[SpeechTokenTiming] = []
    match_cursor = 0
    used_token_ids: set[str] = set()
    for index, token in enumerate(tokens):
        match_index = _next_script_token_match(matches, match_cursor, token.text)
        update: dict[str, object] = {}
        if match_index is not None:
            match = matches[match_index]
            update["char_span"] = (match.start(), match.end())
            match_cursor = match_index + 1
        preferred_id = token.token_id or (
            f"token_{match_index + 1:04d}"
            if match_index is not None
            else f"token_unmapped_{index + 1:04d}"
        )
        token_id = preferred_id
        if token_id in used_token_ids:
            token_id = f"{preferred_id}__{index + 1:04d}"
        used_token_ids.add(token_id)
        update["token_id"] = token_id
        enriched.append(token.model_copy(update=update))

    cursor = 0
    for unit in units:
        unit_id = _unit_value(unit, "unit_id")
        needle = normalize_speech_text(_unit_value(unit, "text"))
        if not unit_id or not needle:
            continue
        claim = _claim_token_text(enriched, cursor, needle)
        if claim is None:
            continue
        start_index, end_index = claim
        for token_index in range(start_index, end_index):
            enriched[token_index] = enriched[token_index].model_copy(
                update={"source_unit_id": unit_id}
            )
        cursor = end_index
    return enriched


def normalize_speech_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char.lower() for char in normalized if char.isalnum())


def _normalize_display_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char.lower() for char in normalized if not char.isspace())


def _unit_value(unit: object, key: str) -> str:
    if isinstance(unit, dict):
        return str(unit.get(key) or "")
    return str(getattr(unit, key, "") or "")


def _claim_token_text(
    tokens: list[SpeechTokenTiming], start_index: int, needle: str
) -> tuple[int, int] | None:
    value = ""
    matched_end: int | None = None
    for end_index in range(start_index, len(tokens)):
        piece = normalize_speech_text(tokens[end_index].text)
        if matched_end is not None:
            if piece:
                return start_index, matched_end
            matched_end = end_index + 1
            continue
        value += piece
        if value == needle:
            matched_end = end_index + 1
            continue
        if len(value) >= len(needle) or not needle.startswith(value):
            break
    return (start_index, matched_end) if matched_end is not None else None


def _next_script_token_match(
    matches: list[re.Match[str]], start_index: int, token_text: str
) -> int | None:
    """Align a possibly sparse historical token stream back to script positions.

    Older normalized alignment artifacts can omit zero-duration punctuation tokens.
    Searching forward by token text, instead of pairing by list index, preserves
    monotonic character spans after any omission.
    """

    needle = _normalize_display_text(token_text)
    if not needle:
        return None
    for index in range(start_index, len(matches)):
        if _normalize_display_text(matches[index].group(0)) == needle:
            return index
    return None


def _anchor_display_tokens(
    script: str, tokens: list[SpeechTokenTiming], limit: float
) -> tuple[list[SpeechTokenTiming], int, int]:
    matches = list(_DISPLAY_TOKEN.finditer(str(script or "")))
    values = [match.group(0) for match in matches]
    if not values:
        return [], 0, 0
    weights = [max(1, len(normalize_speech_text(value))) for value in values]
    anchors = _match_anchors(values, weights, tokens)
    times = _interpolate_token_times(weights, anchors, limit)
    result: list[SpeechTokenTiming] = []
    for index, (match, value, (start, end)) in enumerate(zip(matches, values, times, strict=True)):
        # Provider token times are spoken truth and may legitimately overlap at a
        # sentence boundary. Never force them onto a single global cursor: doing
        # so can collapse the first token of the next sentence to zero duration.
        # Display non-overlap is a CaptionCompositionPlanning concern.
        start = max(0.0, min(limit, start))
        end = max(start, min(limit, end))
        if end > start:
            result.append(
                SpeechTokenTiming(
                    text=value,
                    start=start,
                    end=end,
                    token_id=f"token_{index + 1:04d}",
                    char_span=(match.start(), match.end()),
                )
            )
    matched = sum(1 for span in anchors if span is not None)
    return result, matched, len(values) - matched


def _match_anchors(
    values: list[str], weights: list[int], tokens: list[SpeechTokenTiming]
) -> list[tuple[float, float] | None]:
    """Greedily align display tokens to provider tokens in time order.

    A two-pointer walk pairs equal (normalized) tokens as anchors. When one
    side merges what the other splits — e.g. a v3 TTS token ``天气`` (or ``好，``
    with the punctuation attached) spanning the two display tokens ``天``/``气`` —
    the coarser token anchors the whole finer run, which shares its interval
    subdivided by character weight. On an unresolved mismatch it skips forward on
    whichever side reaches the next match first, within a bounded look-ahead
    window; anything never paired stays unanchored for the caller to
    interpolate. Deterministic and O(n · window)."""

    anchors: list[tuple[float, float] | None] = [None] * len(values)
    script_norm = [normalize_speech_text(value) for value in values]
    provider_norm = [normalize_speech_text(token.text) for token in tokens]
    i = 0
    j = 0
    while i < len(values) and j < len(tokens):
        if not script_norm[i]:
            i += 1
            continue
        if not provider_norm[j]:
            j += 1
            continue
        if script_norm[i] == provider_norm[j]:
            anchors[i] = (tokens[j].start, tokens[j].end)
            i += 1
            j += 1
            continue
        script_run_end = _run_match(script_norm, i, provider_norm[j])
        if script_run_end is not None:
            _distribute_run(anchors, weights, i, script_run_end, tokens[j].start, tokens[j].end)
            i = script_run_end + 1
            j += 1
            continue
        provider_run_end = _run_match(provider_norm, j, script_norm[i])
        if provider_run_end is not None:
            anchors[i] = (tokens[j].start, tokens[provider_run_end].end)
            i += 1
            j = provider_run_end + 1
            continue
        provider_ahead = _lookahead(provider_norm, j + 1, script_norm[i])
        script_ahead = _lookahead(script_norm, i + 1, provider_norm[j])
        if provider_ahead is not None and (
            script_ahead is None or (provider_ahead - j) <= (script_ahead - i)
        ):
            j = provider_ahead
        elif script_ahead is not None:
            i = script_ahead
        else:
            i += 1
    return anchors


def _run_match(sequence: list[str], start: int, target: str) -> int | None:
    """Smallest end index whose ``sequence[start:end + 1]`` normalized-joins to
    ``target``, within the look-ahead window; ``None`` if no prefix leads there."""
    if not target:
        return None
    accumulated = ""
    for index in range(start, min(len(sequence), start + _ANCHOR_WINDOW)):
        accumulated += sequence[index]
        if accumulated == target:
            return index
        if len(accumulated) >= len(target) or not target.startswith(accumulated):
            break
    return None


def _distribute_run(
    anchors: list[tuple[float, float] | None],
    weights: list[int],
    start_index: int,
    end_index: int,
    start: float,
    end: float,
) -> None:
    span_weights = weights[start_index : end_index + 1]
    total = sum(span_weights)
    cursor = float(start)
    for offset, target_index in enumerate(range(start_index, end_index + 1)):
        token_end = (
            float(end)
            if offset == len(span_weights) - 1
            else cursor + (float(end) - float(start)) * span_weights[offset] / total
        )
        anchors[target_index] = (cursor, token_end)
        cursor = token_end


def _lookahead(sequence: list[str], start: int, target: str) -> int | None:
    if not target:
        return None
    for index in range(start, min(len(sequence), start + _ANCHOR_WINDOW)):
        if sequence[index] == target:
            return index
    return None


def _interpolate_token_times(
    weights: list[int], anchors: list[tuple[float, float] | None], limit: float
) -> list[tuple[float, float]]:
    times: list[tuple[float, float]] = [(0.0, 0.0)] * len(weights)
    index = 0
    previous_end = 0.0
    while index < len(weights):
        if anchors[index] is not None:
            times[index] = anchors[index]
            previous_end = anchors[index][1]
            index += 1
            continue
        run_end = index
        while run_end < len(weights) and anchors[run_end] is None:
            run_end += 1
        left = previous_end
        right = anchors[run_end][0] if run_end < len(weights) else limit
        right = max(left, right)
        span_weights = weights[index:run_end]
        total = sum(span_weights)
        cursor = left
        for offset, target in enumerate(range(index, run_end)):
            token_end = (
                right
                if offset == len(span_weights) - 1
                else cursor + (right - left) * span_weights[offset] / total
            )
            times[target] = (cursor, token_end)
            cursor = token_end
        previous_end = right
        index = run_end
    return times


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
