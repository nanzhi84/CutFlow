"""Pure helpers for MiniMax TTS-native forced-alignment subtitles.

Ported (intent, not verbatim) from the original
``backend/app/services/forced_alignment_service.py``. Nothing here loads an ML
model or makes a network call — the MiniMax subtitle fetch lives in the MiniMax
provider; this module only holds the pure conversions used to turn a script into
one-subtitle-per-sentence text and to reshape the returned subtitle segments
into the ASR-shaped dicts (``start``/``end``/``text`` in SECONDS) the narration
alignment node already consumes.
"""

from __future__ import annotations

import re
from typing import Any

# Sentence-ending punctuation (full-width CJK + ASCII). A run of these closes a
# sentence; consecutive markers (e.g. ``……``, ``?!``) stay with their sentence.
# Split only after the LAST char of a run (lookbehind on an end char + negative
# lookahead so we never break *between* consecutive markers).
_SENTENCE_END_CHARS = "。！？；…!?;"
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[" + re.escape(_SENTENCE_END_CHARS) + r"])(?![" + re.escape(_SENTENCE_END_CHARS) + r"])"
)
_WHITESPACE_RE = re.compile(r"\s+")


def split_text_into_lines(script: str) -> str:
    """Split ``script`` into one sentence per line on sentence-ending punctuation.

    The punctuation stays attached to its sentence. Existing whitespace/newlines
    are collapsed to single spaces first, then the text is split on the boundary
    after each run of sentence-ending punctuation. Empty lines are dropped. A
    trailing sentence with no terminal punctuation is kept as its own line.

    Pure function returning the sentences joined by ``"\\n"`` (no trailing
    newline). MiniMax splits subtitle segments on newlines, so feeding this as
    the subtitle text yields one precise-timestamped segment per sentence while
    the spoken audio is unchanged (MiniMax ignores ``\\n`` for speech).
    """
    if not script:
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", str(script)).strip()
    if not collapsed:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(collapsed)
    lines = [part.strip() for part in parts if part and part.strip()]
    return "\n".join(lines)


def _segment_fields(segment: Any) -> tuple[float, float, str] | None:
    """Extract ``(begin_ms, end_ms, text)`` from a MiniMax subtitle segment.

    Accepts dicts (``time_begin``/``time_end``/``text``) or objects exposing the
    same attributes. Returns ``None`` for malformed entries so the caller can
    skip them without raising.
    """

    def _get(key: str, default: Any = None) -> Any:
        if isinstance(segment, dict):
            return segment.get(key, default)
        return getattr(segment, key, default)

    try:
        begin = float(_get("time_begin", 0.0) or 0.0)
        end = float(_get("time_end", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    text = str(_get("text", "") or "").strip()
    return begin, end, text


def subtitle_segments_to_asr_shape(segments: Any) -> list[dict[str, Any]]:
    """Convert MiniMax subtitle segments (ms) into ASR-shaped dicts (seconds).

    Input items carry ``time_begin``/``time_end`` (milliseconds) and ``text``.
    Output items are ``{"start": sec, "end": sec, "text": str}`` — the exact
    shape ``LocalRuntimeAdapter._narration_units_from_segments`` consumes.
    Malformed/empty-text entries are dropped. Pure function.
    """
    if not isinstance(segments, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for segment in segments:
        fields = _segment_fields(segment)
        if fields is None:
            continue
        begin_ms, end_ms, text = fields
        if not text:
            continue
        result.append(
            {
                "start": begin_ms / 1000.0,
                "end": end_ms / 1000.0,
                "text": text,
            }
        )
    return result
