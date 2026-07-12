"""Huazi (emphasis caption) candidate derivation + HuaziPlanningSubagent helpers.

Two responsibilities, both pure (no IO, no randomness) so they unit-test as plain
functions:

1. ``derive_huazi_candidates`` places each emphasis phrase onto the narration
   sentence that contains it (moved out of ``style_planning`` in Caption Display
   v2). The deterministic editing chain no longer derives huazi; the active v2
   path further quantizes and validates these candidates in CaptionWindowPlanning,
   while the frozen v1 path keeps its historical HuaziPlanningSubagent consumer.

2. Parse / validate / finalize the subagent's ID-only selection. The subagent may
   only pick an ``event_id`` + ``layout_box_id`` + ``animation_id`` + ``priority``;
   fonts, colours, coordinates, timing, sfx and rewritten text stay local. The
   validator returns hard errors (fed back for one repair); ``finalize`` then
   applies the deterministic caps (punch limit, adjacency density) and materializes
   the chosen box geometry into ``OverlayEvent`` rects.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from typing import Any

from packages.core.contracts.artifacts import EmphasisHint, OverlayEvent, OverlayRect
from packages.production.pipeline._caption_styles import (
    HUAZI_ANIMATION_DIRECTIONS,
    HUAZI_ANIMATIONS,
)

# Candidate phrase length is measured in *visual* characters (letters / numbers /
# symbols, i.e. skipping whitespace, punctuation and combining marks). Phrases
# outside 2-10 visual chars make poor huazi banners (too terse or too wide).
_MIN_VISUAL_CHARS = 2
_MAX_VISUAL_CHARS = 10
# Never expose more than this many candidates, one per sentence. Raised from 6 so
# a >=5-event emphasis floor has enough headroom after pixel-safety attrition.
HUAZI_MAX_CANDIDATES = 10

# Deterministic caps applied by ``finalize_huazi_plan`` (not repair errors).
PUNCH_MAX = 2
HUAZI_MIN_ADJACENT_GAP = 0.8

# Normal caption safety zone: reserve the worst-case wrapped caption (2 lines,
# ~1.25em line box each) above the caption anchor so huazi boxes never collide
# with normal subtitles.
_CAPTION_LINE_BOX_EM = 1.25
_NORMAL_CAPTION_RESERVED_LINES = 2


def normal_caption_top_y(*, position_y: float, font_size: int, canvas_height: int) -> float:
    """Normalized top edge of the worst-case 2-line normal caption safety zone.

    ``generate_layout_boxes`` drops any huazi box reaching into this zone. The
    normal caption anchor sits at ``position_y`` (normalized); reserve two line
    boxes above it for a worst-case wrapped caption.
    """
    line_fraction = (float(font_size) * _CAPTION_LINE_BOX_EM) / max(1, int(canvas_height))
    reserved = _NORMAL_CAPTION_RESERVED_LINES * line_fraction
    return round(max(0.0, min(1.0, float(position_y)) - reserved), 4)


def _compact_text(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if not ch.isspace())


def _visual_char_count(text: str) -> int:
    """Count meaningful glyphs: letters/numbers/symbols, not spaces/punct/marks."""
    count = 0
    for ch in str(text or ""):
        if unicodedata.category(ch)[0] in {"Z", "C", "M", "P"}:
            continue
        count += 1
    return count


def derive_huazi_candidates(
    emphasis: list[EmphasisHint], units: list[dict]
) -> list[OverlayEvent]:
    """Place each emphasis phrase onto the narration sentence that contains it.

    Deterministic substring match (whitespace/case-insensitive) against the real
    narration timeline; the matched sentence supplies the timing, the phrase itself
    is the overlay text. A phrase matching no sentence is dropped; at most one
    overlay per narration sentence (two phrases sharing a sentence would render as
    same-time banners), so a later phrase whose only match is an already-claimed
    sentence is dropped. Phrases outside the 2-10 visual-char band are filtered out
    here (not left to the subagent), and the whole list is capped at
    ``HUAZI_MAX_CANDIDATES``. Phrases keep the LLM's order.
    """
    events: list[OverlayEvent] = []
    used: set[int] = set()
    for hint in emphasis:
        if len(events) >= HUAZI_MAX_CANDIDATES:
            break
        needle = _compact_text(hint.phrase)
        if not needle:
            continue
        if not (_MIN_VISUAL_CHARS <= _visual_char_count(hint.phrase) <= _MAX_VISUAL_CHARS):
            continue
        for index, unit in enumerate(units):
            if index in used:
                continue
            if needle in _compact_text(str(unit.get("text", ""))):
                used.add(index)
                events.append(
                    OverlayEvent(
                        event_id=f"hz_{len(events) + 1:03d}",
                        start=float(unit.get("start", 0) or 0),
                        end=float(unit.get("end", 0) or 0),
                        text=hint.phrase,
                    )
                )
                break
    return events


# --------------------------------------------------------------------------- #
# HuaziPlanningSubagent selection parsing / validation / finalize
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HuaziPlanChoice:
    event_id: str
    layout_box_id: str
    animation_id: str
    priority: int
    reason: str


# The subagent chooses IDs only. Fonts, colours, coordinates, timing, sfx and
# rewritten text stay local — any of these keys is an overreach that goes back on
# the repair prompt.
_FORBIDDEN_HUAZI_OUTPUT_KEYS = frozenset(
    {
        "sfx_id",
        "sfx",
        "font_id",
        "font_name",
        "font_size",
        "color",
        "primary_color",
        "outline",
        "outline_color",
        "x",
        "y",
        "position",
        "coordinates",
        "rect",
        "text",
        "phrase",
        "start",
        "end",
        "start_sec",
        "end_sec",
        "timeline_start",
        "timeline_end",
    }
)


def _as_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_huazi_plan(
    output: Any,
) -> tuple[list[HuaziPlanChoice], list[str], list[str]]:
    """Parse ``{"huazi": [...]}`` into choices, overreach and shape errors.

    Never raises. Structural errors are returned separately so the caller can repair
    malformed provider output without misreporting it as forbidden-field overreach.
    An explicit empty ``huazi`` list remains a valid "select nothing" response.
    """
    if not isinstance(output, dict):
        return [], [], ["huazi response must be a JSON object"]
    if "huazi" not in output:
        return [], [], ["huazi response must include a 'huazi' array"]
    items = output.get("huazi")
    if not isinstance(items, list):
        return [], [], ["huazi response field 'huazi' must be an array"]

    overreach: list[str] = []
    parse_errors: list[str] = []
    choices: list[HuaziPlanChoice] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            parse_errors.append(f"huazi[{index}] must be an object")
            continue
        overreach.extend(
            f"huazi.{key}" for key in sorted(item) if key in _FORBIDDEN_HUAZI_OUTPUT_KEYS
        )
        event_id = _as_str(item.get("event_id"))
        if not event_id:
            parse_errors.append(f"huazi[{index}].event_id is required")
            continue
        choices.append(
            HuaziPlanChoice(
                event_id=event_id,
                layout_box_id=_as_str(item.get("layout_box_id")),
                animation_id=_as_str(item.get("animation_id")) or "pop_in",
                priority=_as_int(item.get("priority")),
                reason=_as_str(item.get("reason")),
            )
        )
    return choices, sorted(dict.fromkeys(overreach)), parse_errors


def validate_huazi_plan(
    choices: list[HuaziPlanChoice],
    *,
    candidate_events: list[dict],
    boxes_by_event: dict[str, list[dict]],
    overreach_fields: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Local hard constraints on the subagent's ID-only huazi selection.

    Returns human-readable error strings (empty == valid) fed back verbatim on the
    repair prompt. Whitelist membership (event/box/animation), slide-direction
    compatibility with the chosen box, one-event-per-sentence uniqueness and the
    ``HUAZI_MAX_CANDIDATES`` ceiling are hard errors here; the punch cap and
    adjacency density are deterministic post-adjustments applied by ``finalize``.
    """
    errors: list[str] = []
    if overreach_fields:
        errors.append(
            "huazi selection includes forbidden fields: " + ", ".join(overreach_fields)
        )
    event_ids = {
        _as_str(event.get("event_id"))
        for event in candidate_events
        if _as_str(event.get("event_id"))
    }
    seen: set[str] = set()
    for choice in choices:
        if choice.event_id not in event_ids:
            errors.append(f"huazi event_id '{choice.event_id}' is not a known huazi candidate")
            continue
        if choice.event_id in seen:
            errors.append(f"huazi event '{choice.event_id}' is selected more than once")
            continue
        seen.add(choice.event_id)
        boxes = {
            _as_str(box.get("layout_box_id")): box for box in boxes_by_event.get(choice.event_id, [])
        }
        box = boxes.get(choice.layout_box_id)
        if box is None:
            hint = ", ".join(list(boxes)[:8]) or "none"
            errors.append(
                f"huazi layout_box_id '{choice.layout_box_id}' is not a candidate box for event "
                f"'{choice.event_id}'; choose one of: {hint}"
            )
            continue
        if choice.animation_id not in HUAZI_ANIMATIONS:
            errors.append(
                f"huazi animation_id '{choice.animation_id}' is not a known animation candidate"
            )
            continue
        direction = HUAZI_ANIMATION_DIRECTIONS.get(choice.animation_id)
        allowed = [d for d in (box.get("allowed_enter_directions") or [])]
        if direction is not None and direction not in allowed:
            allowed_hint = ", ".join(allowed) or "none"
            errors.append(
                f"huazi animation '{choice.animation_id}' enters from '{direction}' but box "
                f"'{choice.layout_box_id}' only allows: {allowed_hint}"
            )
    if len(seen) > HUAZI_MAX_CANDIDATES:
        errors.append(f"huazi selection exceeds the maximum of {HUAZI_MAX_CANDIDATES} events")
    return errors


@dataclass(frozen=True)
class HuaziFinalizeResult:
    overlay_events: list[OverlayEvent]
    animation_fallbacks: int
    density_drops: int
    choices: list[dict]


def _adjacency_winner(
    earlier: HuaziPlanChoice,
    earlier_event: dict,
    later: HuaziPlanChoice,
    later_event: dict,
) -> HuaziPlanChoice:
    if earlier.priority != later.priority:
        return earlier if earlier.priority > later.priority else later
    earlier_start = float(earlier_event.get("start", 0) or 0)
    later_start = float(later_event.get("start", 0) or 0)
    if earlier_start != later_start:
        return earlier if earlier_start < later_start else later
    return earlier if earlier.event_id <= later.event_id else later


def finalize_huazi_plan(
    choices: list[HuaziPlanChoice],
    *,
    candidate_events: list[dict],
    boxes_by_event: dict[str, list[dict]],
) -> HuaziFinalizeResult:
    """Apply deterministic caps then materialize box geometry into OverlayEvents.

    Assumes ``choices`` already passed ``validate_huazi_plan`` (unique known events,
    known boxes, direction-compatible animations). Two deterministic adjustments,
    recorded as counts rather than repair errors:

    * punch cap — at most ``PUNCH_MAX`` ``punch`` animations survive; excess ones
      (lowest priority first, tie-broken by earlier start then event_id) fall back
      to ``pop_in``.
    * adjacency density — events whose start sits within ``HUAZI_MIN_ADJACENT_GAP``
      seconds of the previously kept event's end are dropped, keeping the higher
      priority one (tie: earlier start, then event_id).
    """
    events_by_id = {_as_str(event.get("event_id")): event for event in candidate_events}
    resolved = list(choices)

    animation_fallbacks = 0
    punches = [choice for choice in resolved if choice.animation_id == "punch"]
    if len(punches) > PUNCH_MAX:
        ranked = sorted(
            punches,
            key=lambda choice: (
                -choice.priority,
                float(events_by_id[choice.event_id].get("start", 0) or 0),
                choice.event_id,
            ),
        )
        keep_ids = {choice.event_id for choice in ranked[:PUNCH_MAX]}
        downgraded: list[HuaziPlanChoice] = []
        for choice in resolved:
            if choice.animation_id == "punch" and choice.event_id not in keep_ids:
                choice = replace(choice, animation_id="pop_in")
                animation_fallbacks += 1
            downgraded.append(choice)
        resolved = downgraded

    ordered = sorted(
        resolved,
        key=lambda choice: (
            float(events_by_id[choice.event_id].get("start", 0) or 0),
            choice.event_id,
        ),
    )
    density_drops = 0
    kept: list[HuaziPlanChoice] = []
    for choice in ordered:
        event = events_by_id[choice.event_id]
        if kept:
            last = kept[-1]
            last_event = events_by_id[last.event_id]
            gap = float(event.get("start", 0) or 0) - float(last_event.get("end", 0) or 0)
            if gap < HUAZI_MIN_ADJACENT_GAP:
                if _adjacency_winner(last, last_event, choice, event) is choice:
                    kept[-1] = choice
                density_drops += 1
                continue
        kept.append(choice)

    overlay_events: list[OverlayEvent] = []
    diagnostics: list[dict] = []
    for choice in kept:
        event = events_by_id[choice.event_id]
        box = next(
            box
            for box in boxes_by_event.get(choice.event_id, [])
            if _as_str(box.get("layout_box_id")) == choice.layout_box_id
        )
        rect = box.get("rect") or {}
        overlay_events.append(
            OverlayEvent(
                event_id=choice.event_id,
                start=float(event.get("start", 0) or 0),
                end=float(event.get("end", 0) or 0),
                text=str(event.get("text") or ""),
                style="emphasis",
                animation_id=choice.animation_id,
                layout_box_id=choice.layout_box_id,
                rect=OverlayRect(
                    x=float(rect.get("x", 0) or 0),
                    y=float(rect.get("y", 0) or 0),
                    w=float(rect.get("w", 0) or 0),
                    h=float(rect.get("h", 0) or 0),
                ),
                text_align=_as_str(box.get("text_align")) or "center",
                priority=choice.priority,
                reason=choice.reason,
                sfx_id="none",
            )
        )
        diagnostics.append(
            {
                "event_id": choice.event_id,
                "text": str(event.get("text") or ""),
                "layout_box_id": choice.layout_box_id,
                "animation_id": choice.animation_id,
                "priority": choice.priority,
                "reason": choice.reason,
            }
        )
    return HuaziFinalizeResult(
        overlay_events=overlay_events,
        animation_fallbacks=animation_fallbacks,
        density_drops=density_drops,
        choices=diagnostics,
    )
