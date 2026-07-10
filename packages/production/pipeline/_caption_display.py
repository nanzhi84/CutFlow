"""Deterministic caption display compiler (Caption Display v2, issue #188).

Pure, IO-free planner that turns raw narration units into display-ready caption
cues (already line-broken) plus, in mixed mode, a time-deduplicated normal track
that yields to the huazi (emphasis) events.

The module is intentionally self-contained: it holds its own dataclasses instead
of importing ``packages.core.contracts`` so the render/planning integration layer
owns the conversion to the persisted ``CaptionDisplayPlan.v1`` artifact.

Pipeline (fixed order):
    C1  normalize + merge   -- fold pure-punctuation / tiny cues into neighbours
    C3  per-cue DP wrap      -- pick the min-penalty legal 1/2-line split
    C4  over-long time split -- split cues that cannot fit two lines
    E   pure-time dedup       -- punch huazi windows out of the normal track

Width measurement is injected (``measure(text) -> px``) so the compiler stays
deterministic and testable without font IO.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

# --- tunables -------------------------------------------------------------------

_WIDTH_SAFETY = 0.95  # D11: hand-broken lines must not be re-wrapped by libass
_MERGE_GAP_SEC = 0.3  # tiny cues fold into a neighbour only within this gap
_SHORT_CUE_MEANINGFUL = 3  # "tiny" cue = <= this many meaningful chars
_MIN_DISPLAY_SEC = 0.6  # cues/fragments shorter than this are unreadable -> drop
_MAX_SPLIT_DEPTH = 8  # recursion guard for pathological unbreakable text

# Line-head forbidden characters (禁则): a break must not leave these leading a
# line (closing brackets / terminal punctuation float back onto the prior line).
_FORBID_LINE_START = set(
    "，。！？；：、）》】」』｝〕…”’"  # full-width closers / terminals
    ",.!?;:%)]}\"'"  # half-width analogues (straight quotes are ambiguous -> both sets)
)
# Line-tail forbidden characters: an opening bracket / quote must not end a line.
_FORBID_LINE_END = set(
    "（《【「『｛〔“‘"  # full-width openers
    "([{\"'"  # half-width analogues
)

# Break-quality tiers. A break right after a sentence terminal is free; after a
# soft pause it is nearly free; anywhere else costs a full point. This is the
# only place the wrap "prefers" punctuation -- no jieba, fully deterministic.
_SENTENCE_END = set("。！？；!?;")
_PAUSE = set("，、：,:")
_IMBALANCE_WEIGHT = 3.0  # width imbalance dominates so 16+2 loses to 9+9

# Protected tokens (D12): a break must never fall strictly inside one. No brand
# words (no data source). Prices / sizes / speeds / percentages / runs of
# latin-digits / dates.
_TOKEN_PATTERNS = [
    re.compile(r"\d+(?:\.\d+)?(?:元起?|折|%|％)"),
    re.compile(r"\d+(?:\.\d+)?[xX倍]"),
    re.compile(r"\d+(?:\.\d+)?\s*[cmk]?m?[×xX]\s*\d+(?:\.\d+)?\s*[cmk]?m?"),
    re.compile(r"[A-Za-z0-9.\-]+"),
    re.compile(r"\d+月\d+日?"),
]


# --- public result types (self-contained; integration layer converts these) ----


@dataclass
class CaptionCueData:
    """A display-ready normal caption cue (already line-broken)."""

    start: float
    end: float
    lines: list[str]
    source_unit_ids: list[int | str]
    suppressed_by: str | None = None


@dataclass
class CaptionDisplayDiagnostics:
    """Counters mirroring the persisted ``CaptionDisplayDiagnostics`` contract.

    ``animation_fallbacks`` is owned by the planning/render layer, not this
    compiler, and stays at its default here.
    """

    merged_units: int = 0
    split_cues: int = 0
    suppressed_duplicates: int = 0
    dropped_fragments: int = 0
    animation_fallbacks: int = 0
    font_metrics_source: str = "hmtx"


@dataclass
class CaptionDisplayResult:
    normal_cues: list[CaptionCueData]
    suppressed_cues: list[CaptionCueData]
    emphasis_events: list[dict]
    diagnostics: CaptionDisplayDiagnostics


# --- internal working state -----------------------------------------------------


@dataclass
class _WorkCue:
    """Mutable pre-wrap cue carrying raw text (never the caller's unit dicts)."""

    start: float
    end: float
    text: str
    source_unit_ids: list[int] = field(default_factory=list)


@dataclass
class _Ctx:
    measure: Callable[[str], float]
    avail: float
    diag: CaptionDisplayDiagnostics


# --- character helpers ----------------------------------------------------------


def _is_meaningful(char: str) -> bool:
    """A character that carries reading content: not whitespace, not punctuation."""
    if char.isspace():
        return False
    return unicodedata.category(char)[0] != "P"


def _count_meaningful(text: str) -> int:
    return sum(1 for ch in text if _is_meaningful(ch))


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            if match.end() > match.start():
                spans.append((match.start(), match.end()))
    return spans


def _inside_token(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < index < end for start, end in spans)


def _break_penalty(prev_char: str) -> float:
    if prev_char in _SENTENCE_END:
        return 0.0
    if prev_char in _PAUSE:
        return 0.2
    return 1.0


# --- C1: normalize + merge ------------------------------------------------------


def _merge_back(prev: _WorkCue, cur: _WorkCue) -> None:
    prev.end = cur.end
    prev.text = prev.text + cur.text
    prev.source_unit_ids = prev.source_unit_ids + cur.source_unit_ids


def _merge_forward(cur: _WorkCue, nxt: _WorkCue) -> None:
    nxt.start = cur.start
    nxt.text = cur.text + nxt.text
    nxt.source_unit_ids = cur.source_unit_ids + nxt.source_unit_ids


def _merge_cues(cues: list[_WorkCue], diag: CaptionDisplayDiagnostics) -> list[_WorkCue]:
    result: list[_WorkCue] = []
    i = 0
    n = len(cues)
    while i < n:
        cur = cues[i]
        meaningful = _count_meaningful(cur.text)
        is_pure = meaningful == 0
        if result:
            prev = result[-1]
            gap_prev = cur.start - prev.end
            # Pure-punctuation cues always fold back and extend the prior cue's
            # end; tiny cues fold back only when they abut the prior cue.
            if is_pure:
                _merge_back(prev, cur)
                diag.merged_units += 1
                i += 1
                continue
            if meaningful <= _SHORT_CUE_MEANINGFUL and gap_prev < _MERGE_GAP_SEC:
                _merge_back(prev, cur)
                diag.merged_units += 1
                i += 1
                continue
        # A leading tiny/pure cue with no absorbing predecessor folds forward.
        if i + 1 < n:
            nxt = cues[i + 1]
            gap_next = nxt.start - cur.end
            if (is_pure or meaningful <= _SHORT_CUE_MEANINGFUL) and gap_next < _MERGE_GAP_SEC:
                _merge_forward(cur, nxt)
                diag.merged_units += 1
                i += 1
                continue
        result.append(cur)
        i += 1
    return result


# --- C3: DP line wrap -----------------------------------------------------------


def _legal_break(text: str, index: int, spans: list[tuple[int, int]]) -> bool:
    """Hard constraints for a candidate break (everything except line width)."""
    left = text[:index].strip()
    right = text[index:].strip()
    if not left or not right:
        return False
    if left[-1] in _FORBID_LINE_END:
        return False
    if right[0] in _FORBID_LINE_START:
        return False
    if _inside_token(index, spans):
        return False
    if _count_meaningful(left) < 1:
        return False
    # A stranded 1-char tail reads badly; require >= 2 meaningful chars on line 2.
    if _count_meaningful(right) < 2:
        return False
    return True


def _dp_break(text: str, ctx: _Ctx, *, relax_width: bool = False) -> list[str] | None:
    """Return 1 or 2 display lines, or None when the text cannot fit two lines.

    Enumerates every legal break and keeps the minimum-penalty split. Penalty =
    ``_IMBALANCE_WEIGHT * width_imbalance + break_penalty``; ties resolve by the
    earliest break index for determinism. ``relax_width`` drops the per-line
    width veto (last-resort path so C4 recursion always terminates).
    """
    text = text.strip()
    if not text:
        return None
    if not relax_width and ctx.measure(text) <= ctx.avail:
        return [text]  # single line wins whenever it fits

    spans = _protected_spans(text)
    best_key: tuple[float, int] | None = None
    best_lines: list[str] | None = None
    for index in range(1, len(text)):
        if not _legal_break(text, index, spans):
            continue
        left = text[:index].strip()
        right = text[index:].strip()
        w1 = ctx.measure(left)
        w2 = ctx.measure(right)
        if not relax_width and (w1 > ctx.avail or w2 > ctx.avail):
            continue
        imbalance = abs(w1 - w2) / max(w1, w2, 1e-9)
        penalty = _IMBALANCE_WEIGHT * imbalance + _break_penalty(left[-1])
        key = (penalty, index)
        if best_key is None or key < best_key:
            best_key = key
            best_lines = [left, right]
    return best_lines


# --- C4: over-long time split ---------------------------------------------------


def _find_time_split_index(text: str, spans: list[tuple[int, int]]) -> int | None:
    """Best split point for an over-long cue: punctuation first, then balance."""
    candidates = [k for k in range(1, len(text)) if _legal_break(text, k, spans)]
    if not candidates:
        return None
    mid = len(text) / 2.0

    def rank(k: int) -> tuple[int, float, int]:
        prev_char = text[:k].strip()[-1]
        if prev_char in _SENTENCE_END:
            punct = 0
        elif prev_char in _PAUSE:
            punct = 1
        else:
            punct = 2
        return (punct, abs(k - mid), k)

    candidates.sort(key=rank)
    return candidates[0]


def _visible_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _time_split(cue: _WorkCue, index: int) -> tuple[_WorkCue, _WorkCue]:
    """Split a cue in two at ``index``; time is shared by visible-char share.

    Total duration is preserved exactly. Each side is clamped to >= 0.6s when the
    whole cue is long enough to allow it; both sides inherit the same source ids.
    """
    total = cue.end - cue.start
    left_visible = _visible_len(cue.text[:index])
    total_visible = _visible_len(cue.text) or 1
    left_dur = (left_visible / total_visible) * total
    if total >= 2 * _MIN_DISPLAY_SEC:
        left_dur = min(max(left_dur, _MIN_DISPLAY_SEC), total - _MIN_DISPLAY_SEC)
    split_time = cue.start + left_dur
    left = _WorkCue(cue.start, split_time, cue.text[:index].strip(), list(cue.source_unit_ids))
    right = _WorkCue(split_time, cue.end, cue.text[index:].strip(), list(cue.source_unit_ids))
    return left, right


def _layout_cue(cue: _WorkCue, ctx: _Ctx, depth: int = 0) -> list[CaptionCueData]:
    lines = _dp_break(cue.text, ctx)
    if lines is not None:
        return [CaptionCueData(cue.start, cue.end, lines, list(cue.source_unit_ids))]

    spans = _protected_spans(cue.text)
    index = _find_time_split_index(cue.text, spans)
    if index is None or depth >= _MAX_SPLIT_DEPTH:
        # Unbreakable / too deep: emit a best-effort (possibly wide) result rather
        # than recurse forever. Width overflow here is a diagnosed edge, not silent.
        forced = _dp_break(cue.text, ctx, relax_width=True) or [cue.text.strip()]
        return [CaptionCueData(cue.start, cue.end, forced, list(cue.source_unit_ids))]

    left, right = _time_split(cue, index)
    ctx.diag.split_cues += 1
    return _layout_cue(left, ctx, depth + 1) + _layout_cue(right, ctx, depth + 1)


# --- E: pure-time dedup ---------------------------------------------------------


def _dedup(
    normal_cues: list[CaptionCueData],
    events: list[dict],
    diag: CaptionDisplayDiagnostics,
) -> tuple[list[CaptionCueData], list[CaptionCueData]]:
    """Punch each huazi window out of the overlapping normal cues.

    Text/lines are never touched -- only time. A fully-covered cue is suppressed
    whole (``suppressed_by`` = event id); a partially-covered cue is trimmed to
    its uncovered fragment(s). Trimmed fragments shorter than 0.6s are dropped
    (counted, not suppressed). Events apply in (start, end, id) order so a cue
    already split by one event is further trimmed by the next. Deterministic.
    """
    working = [
        {
            "start": cue.start,
            "end": cue.end,
            "lines": cue.lines,
            "sids": cue.source_unit_ids,
            "origin": i,
        }
        for i, cue in enumerate(normal_cues)
    ]
    suppressed: list[CaptionCueData] = []
    affected: set[int] = set()

    ordered = sorted(
        events,
        key=lambda ev: (
            float(ev.get("start") or 0.0),
            float(ev.get("end") or 0.0),
            str(ev.get("event_id") or ""),
        ),
    )
    for ev in ordered:
        e_start = float(ev.get("start") or 0.0)
        e_end = float(ev.get("end") or 0.0)
        event_id = ev.get("event_id")
        if e_end <= e_start:
            continue
        remaining: list[dict] = []
        for cue in working:
            c_start = cue["start"]
            c_end = cue["end"]
            if c_end <= e_start or c_start >= e_end:
                remaining.append(cue)
                continue
            affected.add(cue["origin"])
            if c_start >= e_start and c_end <= e_end:
                suppressed.append(
                    CaptionCueData(
                        c_start, c_end, list(cue["lines"]), list(cue["sids"]),
                        suppressed_by=event_id,
                    )
                )
                continue
            fragments: list[tuple[float, float]] = []
            if c_start < e_start:
                fragments.append((c_start, e_start))
            if c_end > e_end:
                fragments.append((e_end, c_end))
            for f_start, f_end in fragments:
                if f_end - f_start < _MIN_DISPLAY_SEC:
                    diag.dropped_fragments += 1
                    continue
                remaining.append(
                    {
                        "start": f_start,
                        "end": f_end,
                        "lines": cue["lines"],
                        "sids": cue["sids"],
                        "origin": cue["origin"],
                    }
                )
        working = remaining

    diag.suppressed_duplicates += len(affected)
    kept = [
        CaptionCueData(c["start"], c["end"], list(c["lines"]), list(c["sids"]))
        for c in working
    ]
    kept.sort(key=lambda c: (c.start, c.end))
    suppressed.sort(key=lambda c: (c.start, c.end))
    return kept, suppressed


# --- entry point ----------------------------------------------------------------


def _build_work_cues(units: list[dict]) -> list[_WorkCue]:
    """Copy units into working cues; empties are skipped, unit index is the id."""
    cues: list[_WorkCue] = []
    for idx, unit in enumerate(units):
        text = str(unit.get("text", "") or "").strip()
        if not text:
            continue
        start = float(unit.get("start", 0.0) or 0.0)
        end = float(unit.get("end", 0.0) or 0.0)
        cues.append(_WorkCue(start, end, text, [idx]))
    return cues


def compile_caption_display(
    *,
    units: list[dict],
    resolution: tuple[int, int],
    margin_l: int,
    margin_r: int,
    measure: Callable[[str], float],
    metrics_source: str,
    normal_enabled: bool,
    emphasis_enabled: bool,
    overlay_events: list[dict],
) -> CaptionDisplayResult:
    """Compile display-ready caption cues. ``units``/``overlay_events`` are read-only."""
    diag = CaptionDisplayDiagnostics(font_metrics_source=metrics_source)
    width = int(resolution[0]) if resolution else 0
    avail = max(1.0, (width - margin_l - margin_r) * _WIDTH_SAFETY)
    ctx = _Ctx(measure=measure, avail=avail, diag=diag)

    normal_cues: list[CaptionCueData] = []
    if normal_enabled:
        work = _merge_cues(_build_work_cues(units), diag)
        for cue in work:
            normal_cues.extend(_layout_cue(cue, ctx))

    emphasis_events = list(overlay_events) if emphasis_enabled else []

    suppressed_cues: list[CaptionCueData] = []
    if normal_enabled and emphasis_enabled and emphasis_events:
        normal_cues, suppressed_cues = _dedup(normal_cues, emphasis_events, diag)

    normal_cues.sort(key=lambda c: (c.start, c.end))
    return CaptionDisplayResult(
        normal_cues=normal_cues,
        suppressed_cues=suppressed_cues,
        emphasis_events=emphasis_events,
        diagnostics=diag,
    )


def compile_planned_caption_display(
    *,
    caption_windows: dict,
    normal_enabled: bool,
    emphasis_enabled: bool,
    overlay_events: list[dict],
) -> CaptionDisplayResult:
    """Materialize an already-planned caption track for the renderer.

    ``CaptionWindowPlanning`` owns cue merge/split/wrap and publishes authoritative
    frame windows.  This function deliberately performs no text layout and no font
    IO: it only converts the persisted frame windows to seconds and, in mixed mode,
    applies the existing deterministic pure-time suppression policy.
    """

    fps = max(1, int(caption_windows.get("fps") or 30))
    diagnostics_payload = (
        caption_windows.get("diagnostics")
        if isinstance(caption_windows.get("diagnostics"), dict)
        else {}
    )
    diag = CaptionDisplayDiagnostics(
        merged_units=int(diagnostics_payload.get("merged_units") or 0),
        split_cues=int(diagnostics_payload.get("split_cues") or 0),
        font_metrics_source=str(
            diagnostics_payload.get("font_metrics_source") or "eaw_fallback"
        ),
    )

    normal_cues: list[CaptionCueData] = []
    if normal_enabled:
        for window in caption_windows.get("normal_windows") or []:
            if not isinstance(window, dict):
                continue
            start_frame = max(0, int(window.get("start_frame") or 0))
            end_frame = max(start_frame, int(window.get("end_frame") or 0))
            if end_frame <= start_frame:
                continue
            lines = [str(line) for line in (window.get("lines") or []) if str(line)]
            if not lines:
                continue
            source_ids = [
                item for item in (window.get("source_unit_ids") or []) if item is not None
            ]
            normal_cues.append(
                CaptionCueData(
                    start=start_frame / fps,
                    end=end_frame / fps,
                    lines=lines,
                    source_unit_ids=source_ids,
                )
            )

    emphasis_events = list(overlay_events) if emphasis_enabled else []
    suppressed_cues: list[CaptionCueData] = []
    if normal_enabled and emphasis_enabled and emphasis_events:
        normal_cues, suppressed_cues = _dedup(normal_cues, emphasis_events, diag)

    normal_cues.sort(key=lambda cue: (cue.start, cue.end, tuple(map(str, cue.source_unit_ids))))
    return CaptionDisplayResult(
        normal_cues=normal_cues,
        suppressed_cues=suppressed_cues,
        emphasis_events=emphasis_events,
        diagnostics=diag,
    )
