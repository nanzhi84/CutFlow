"""Pure parse/validate/materialize helpers for PostProcessAgentPlanning."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from packages.core.contracts.artifacts import OverlayEvent, OverlayRect
from packages.production.pipeline._caption_visual_safety import EMPHASIS_MIN_EVENTS

# Once at least the floor of selectable events is offered, PostProcess must commit to
# a 5-8 event band (below the floor it must take them all); this is the selection-side
# guard that pairs with the >=5 emphasis floor enforced upstream.
_CAPTION_MAX_EVENTS = 8


@dataclass(frozen=True)
class PostProcessCaptionChoice:
    event_id: str
    caption_option_id: str
    priority: int
    reason: str


@dataclass(frozen=True)
class PostProcessSelection:
    bgm_id: str | None
    caption_choices: list[PostProcessCaptionChoice] = field(default_factory=list)
    analysis: str = ""
    overreach_fields: tuple[str, ...] = ()


_FORBIDDEN_TOP_LEVEL = frozenset(
    {
        "font_id",
        "font_name",
        "font_size",
        "color",
        "style",
        "style_plan",
        "overlay_events",
        "timeline",
    }
)
_FORBIDDEN_CHOICE_FIELDS = frozenset(
    {
        "anchor_id",
        "layout_box_id",
        "animation_id",
        "typography_variant_id",
        "start",
        "end",
        "start_frame",
        "end_frame",
        "rect",
        "x",
        "y",
        "font_id",
        "font_size",
        "color",
        "text",
        "sfx_id",
    }
)
_ALLOWED_TOP_LEVEL = frozenset({"bgm_id", "caption_choices", "analysis"})
_ALLOWED_CHOICE_FIELDS = frozenset({"event_id", "caption_option_id", "priority", "reason"})


def parse_postprocess_selection(output: Any) -> tuple[PostProcessSelection, list[str]]:
    """Parse the exact three-field provider contract; never accepts aliases."""

    if not isinstance(output, dict):
        return PostProcessSelection(bgm_id=None), ["postprocess response must be a JSON object"]
    errors: list[str] = []
    for required in ("bgm_id", "caption_choices", "analysis"):
        if required not in output:
            errors.append(f"postprocess response must include '{required}'")
    unknown_top = sorted(set(output) - _ALLOWED_TOP_LEVEL)
    if unknown_top:
        errors.append("postprocess response includes unknown fields: " + ", ".join(unknown_top))
    raw_choices = output.get("caption_choices")
    if not isinstance(raw_choices, list):
        errors.append("postprocess response field 'caption_choices' must be an array")
        raw_choices = []
    if not isinstance(output.get("analysis"), str):
        errors.append("postprocess response field 'analysis' must be a string")
    raw_bgm_id = output.get("bgm_id")
    if raw_bgm_id is not None and not isinstance(raw_bgm_id, str):
        errors.append("postprocess response field 'bgm_id' must be null or a string")

    overreach = [key for key in sorted(output) if key in _FORBIDDEN_TOP_LEVEL]
    choices: list[PostProcessCaptionChoice] = []
    for index, item in enumerate(raw_choices):
        if not isinstance(item, dict):
            errors.append(f"caption_choices[{index}] must be an object")
            continue
        overreach.extend(
            f"caption_choices.{key}" for key in sorted(item) if key in _FORBIDDEN_CHOICE_FIELDS
        )
        unknown_choice = sorted(set(item) - _ALLOWED_CHOICE_FIELDS)
        if unknown_choice:
            errors.append(
                f"caption_choices[{index}] includes unknown fields: " + ", ".join(unknown_choice)
            )
        for required in ("event_id", "caption_option_id", "priority", "reason"):
            if required not in item:
                errors.append(f"caption_choices[{index}].{required} is required")
        raw_event_id = item.get("event_id")
        raw_option_id = item.get("caption_option_id")
        raw_reason = item.get("reason")
        event_id = raw_event_id.strip() if isinstance(raw_event_id, str) else ""
        option_id = raw_option_id.strip() if isinstance(raw_option_id, str) else ""
        if not isinstance(raw_event_id, str):
            errors.append(f"caption_choices[{index}].event_id must be a string")
        if not isinstance(raw_option_id, str):
            errors.append(f"caption_choices[{index}].caption_option_id must be a string")
        if not isinstance(raw_reason, str):
            errors.append(f"caption_choices[{index}].reason must be a string")
        if not event_id:
            errors.append(f"caption_choices[{index}].event_id is required")
        if not option_id:
            errors.append(f"caption_choices[{index}].caption_option_id is required")
        if not event_id or not option_id:
            continue
        raw_priority = item.get("priority")
        if type(raw_priority) is not int:
            errors.append(f"caption_choices[{index}].priority must be an integer")
            priority = -1
        else:
            priority = raw_priority
        if type(raw_priority) is int and (priority < 0 or priority > 100):
            errors.append(f"caption_choices[{index}].priority must be between 0 and 100")
        choices.append(
            PostProcessCaptionChoice(
                event_id=event_id,
                caption_option_id=option_id,
                priority=priority,
                reason=raw_reason if isinstance(raw_reason, str) else "",
            )
        )
    selection = PostProcessSelection(
        bgm_id=raw_bgm_id.strip() if isinstance(raw_bgm_id, str) else None,
        caption_choices=choices,
        analysis=str(output.get("analysis") or ""),
        overreach_fields=tuple(dict.fromkeys(overreach)),
    )
    return selection, errors


def unwrap_postprocess_provider_output(output: Any) -> tuple[dict, list[str]]:
    """Accept direct selection or the exact DashScope ``content``/``intent`` envelope."""

    if not isinstance(output, dict):
        return {}, ["postprocess provider output must be a JSON object"]
    if "content" not in output and "intent" not in output:
        return output, []

    errors: list[str] = []
    expected = {"content", "intent"}
    missing = sorted(expected - set(output))
    unknown = sorted(set(output) - expected)
    if missing:
        errors.append("postprocess provider envelope is missing fields: " + ", ".join(missing))
    if unknown:
        errors.append(
            "postprocess provider envelope includes unknown fields: " + ", ".join(unknown)
        )
    if not isinstance(output.get("content"), str):
        errors.append("postprocess provider envelope field 'content' must be a string")
    intent = output.get("intent")
    if not isinstance(intent, dict):
        errors.append("postprocess provider envelope field 'intent' must be an object")
        return {}, errors
    return intent, errors


def validate_postprocess_selection(
    selection: PostProcessSelection,
    *,
    caption_windows: dict,
    bgm_candidates: list[dict],
    bgm_enabled: bool,
    emphasis_enabled: bool,
) -> list[str]:
    errors: list[str] = []
    if selection.overreach_fields:
        errors.append(
            "postprocess selection includes forbidden fields: "
            + ", ".join(selection.overreach_fields)
        )

    bgm_ids = {
        _as_str(candidate.get("candidate_id"))
        for candidate in bgm_candidates
        if _as_str(candidate.get("candidate_id"))
    }
    if not bgm_enabled and selection.bgm_id is not None:
        errors.append("bgm_id must be null when BGM is disabled")
    elif selection.bgm_id is not None and selection.bgm_id not in bgm_ids:
        errors.append(f"bgm_id '{selection.bgm_id}' is not a known BGM candidate")

    windows_by_id = {
        _as_str(window.get("event_id")): window
        for window in (caption_windows.get("emphasis_windows") or [])
        if isinstance(window, dict) and _as_str(window.get("event_id"))
    }
    selectable_count = sum(
        1
        for window in (caption_windows.get("emphasis_windows") or [])
        if isinstance(window, dict) and window.get("caption_options")
    )
    seen: set[str] = set()
    if len(selection.caption_choices) > _CAPTION_MAX_EVENTS:
        errors.append(f"caption_choices must not exceed {_CAPTION_MAX_EVENTS} events")
    selected_windows: list[tuple[int, int, str, str]] = []
    hero_count = 0
    for choice in selection.caption_choices:
        if not emphasis_enabled:
            errors.append("caption_choices must be empty when emphasis captions are disabled")
            break
        window = windows_by_id.get(choice.event_id)
        if window is None:
            errors.append(f"caption event_id '{choice.event_id}' is not a known event")
            continue
        if choice.event_id in seen:
            errors.append(f"caption event '{choice.event_id}' is selected more than once")
            continue
        seen.add(choice.event_id)
        option_ids = {
            _as_str(option.get("caption_option_id"))
            for option in (window.get("caption_options") or [])
            if isinstance(option, dict)
        }
        if choice.caption_option_id not in option_ids:
            errors.append(
                f"caption_option_id '{choice.caption_option_id}' is not valid for "
                f"event '{choice.event_id}'"
            )
            continue
        option = next(
            item
            for item in (window.get("caption_options") or [])
            if _as_str(item.get("caption_option_id")) == choice.caption_option_id
        )
        if _as_str(option.get("visual_preset_id")) == "hero":
            hero_count += 1
        selected_windows.append(
            (
                int(window.get("start_frame") or 0),
                int(window.get("end_frame") or 0),
                choice.event_id,
                choice.caption_option_id,
            )
        )
    if hero_count > 2:
        errors.append("caption choices may use hero at most 2 times")
    min_gap_frames = int(math.ceil(0.8 * max(1, int(caption_windows.get("fps") or 30))))
    selected_windows.sort(key=lambda item: (item[0], item[1], item[2]))
    for previous, current in zip(selected_windows, selected_windows[1:]):
        gap = current[0] - previous[1]
        if gap < min_gap_frames:
            errors.append(
                f"caption events '{previous[2]}' and '{current[2]}' must be non-overlapping "
                "with at least 0.8s gap"
            )
    if emphasis_enabled and selectable_count > 0:
        chosen = len(seen)
        if selectable_count >= EMPHASIS_MIN_EVENTS:
            if not (EMPHASIS_MIN_EVENTS <= chosen <= _CAPTION_MAX_EVENTS):
                errors.append(
                    f"当前有 {selectable_count} 个可选花字事件，必须选择 "
                    f"{EMPHASIS_MIN_EVENTS} 到 {_CAPTION_MAX_EVENTS} 个（现选 {chosen} 个）"
                )
        elif chosen != selectable_count:
            errors.append(
                f"当前有 {selectable_count} 个可选花字事件（不足 {EMPHASIS_MIN_EVENTS} 个），"
                f"必须全部选择（现选 {chosen} 个）"
            )
    return errors


def materialize_overlay_events(
    selection: PostProcessSelection, *, caption_windows: dict
) -> tuple[list[OverlayEvent], list[dict]]:
    fps = max(1, int(caption_windows.get("fps") or 30))
    windows_by_id = {
        _as_str(window.get("event_id")): window
        for window in (caption_windows.get("emphasis_windows") or [])
        if isinstance(window, dict) and _as_str(window.get("event_id"))
    }
    events: list[OverlayEvent] = []
    diagnostics: list[dict] = []
    for choice in selection.caption_choices:
        window = windows_by_id[choice.event_id]
        option = next(
            item
            for item in window.get("caption_options") or []
            if _as_str(item.get("caption_option_id")) == choice.caption_option_id
        )
        anchor_id = _as_str(option.get("anchor_id"))
        anchor = next(
            item
            for item in window.get("anchor_candidates") or []
            if _as_str(item.get("anchor_id")) == anchor_id
        )
        rect = anchor.get("rect") or {}
        visual_preset_id = _as_str(option.get("visual_preset_id")) or None
        event = OverlayEvent(
            event_id=choice.event_id,
            start=int(window.get("start_frame") or 0) / fps,
            end=int(window.get("end_frame") or 0) / fps,
            text=str(window.get("text") or ""),
            style=visual_preset_id or "emphasis",
            animation_id=_as_str(option.get("animation_id")) or "none",
            layout_box_id=anchor_id,
            rect=OverlayRect(
                x=float(rect.get("x") or 0.0),
                y=float(rect.get("y") or 0.0),
                w=float(rect.get("w") or 0.0),
                h=float(rect.get("h") or 0.0),
            ),
            text_align=_as_str(anchor.get("text_align")) or "center",
            priority=choice.priority,
            reason=choice.reason,
            sfx_id=(
                _sfx_id_for_option(option, choice.event_id)
                if visual_preset_id is not None
                else "none"
            ),
            visual_preset_id=visual_preset_id,
        )
        events.append(event)
        diagnostics.append(
            {
                "event_id": choice.event_id,
                "caption_option_id": choice.caption_option_id,
                "priority": choice.priority,
                "reason": choice.reason,
            }
        )
    events.sort(key=lambda event: (event.start, event.end, event.event_id or ""))
    diagnostics.sort(key=lambda item: item["event_id"])
    return events, diagnostics


def _sfx_id_for_option(option: dict, event_id: str) -> str:
    preset_id = _as_str(option.get("visual_preset_id"))
    if preset_id == "hero":
        checksum = sum(ord(char) for char in event_id)
        return "asset_sfx_whoosh" if checksum % 2 else "asset_sfx_impact"
    if preset_id == "emphasis":
        return "asset_sfx_ding"
    return "none"


def _as_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""
