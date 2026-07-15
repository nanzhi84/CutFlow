"""Pure parse/validate/materialize helpers for PostProcessAgentPlanning."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from packages.core.contracts.artifacts import OverlayEvent, OverlayRect
from packages.production.pipeline._caption_window_planner import (
    EMPHASIS_EVENT_GAP_SEC,
    max_feasible_emphasis_count,
)

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
    seen: set[str] = set()
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
    return errors


@dataclass(frozen=True)
class _SolverCandidate:
    window: dict
    choice: PostProcessCaptionChoice
    option: dict
    nonhero_fallback: dict | None
    requested_hero: bool
    forced_hero: bool


def solve_postprocess_selection(
    selection: PostProcessSelection,
    *,
    caption_windows: dict,
    bgm_candidates: list[dict],
    bgm_enabled: bool,
    emphasis_enabled: bool,
    deterministic_fallback: bool = False,
) -> tuple[PostProcessSelection, dict]:
    """Turn semantic ranks/options into the maximum locally legal selection.

    The LLM never owns time legality, event count, or the hero cap. Invalid or
    missing option IDs fall back only for that event; conflicts prune the lower
    ranked event while preserving every other legal choice. A valid BGM ID is
    carried independently from all caption repair.
    """

    bgm_ids = {
        _as_str(candidate.get("candidate_id"))
        for candidate in bgm_candidates
        if _as_str(candidate.get("candidate_id"))
    }
    invalid_bgm_id = None
    if not bgm_enabled:
        bgm_id = None
    elif selection.bgm_id is None or selection.bgm_id in bgm_ids:
        bgm_id = selection.bgm_id
    else:
        invalid_bgm_id = selection.bgm_id
        bgm_id = None

    selectable_windows = (
        [
            window
            for window in (caption_windows.get("emphasis_windows") or [])
            if isinstance(window, dict) and window.get("caption_options")
        ]
        if emphasis_enabled
        else []
    )
    windows_by_id = {
        _as_str(window.get("event_id")): window
        for window in selectable_windows
        if _as_str(window.get("event_id"))
    }
    requested_by_id: dict[str, PostProcessCaptionChoice] = {}
    unknown_event_ids: list[str] = []
    for choice in selection.caption_choices:
        if choice.event_id not in windows_by_id:
            unknown_event_ids.append(choice.event_id)
            continue
        current = requested_by_id.get(choice.event_id)
        if current is None or choice.priority > current.priority:
            requested_by_id[choice.event_id] = choice

    defaulted_option_event_ids: list[str] = []
    candidates: list[_SolverCandidate] = []
    for window in selectable_windows:
        event_id = _as_str(window.get("event_id"))
        options = sorted(
            [item for item in (window.get("caption_options") or []) if isinstance(item, dict)],
            key=lambda item: (
                _as_str(item.get("visual_preset_id")) == "hero",
                _as_str(item.get("caption_option_id")),
            ),
        )
        if not options:
            continue
        option_by_id = {
            _as_str(option.get("caption_option_id")): option
            for option in options
            if _as_str(option.get("caption_option_id"))
        }
        nonhero_fallback = next(
            (option for option in options if _as_str(option.get("visual_preset_id")) != "hero"),
            None,
        )
        requested = requested_by_id.get(event_id)
        option = option_by_id.get(requested.caption_option_id) if requested else None
        if option is None:
            option = nonhero_fallback or options[0]
            defaulted_option_event_ids.append(event_id)
        priority = min(100, max(0, requested.priority if requested else 0))
        reason = requested.reason if requested else "本地安全默认选项"
        if requested is not None and requested.caption_option_id not in option_by_id:
            reason = (reason + "；无效选项已回退本地安全默认").strip("；")
        chosen = PostProcessCaptionChoice(
            event_id=event_id,
            caption_option_id=_as_str(option.get("caption_option_id")),
            priority=priority,
            reason=reason,
        )
        requested_hero = _as_str(option.get("visual_preset_id")) == "hero"
        candidates.append(
            _SolverCandidate(
                window=window,
                choice=chosen,
                option=option,
                nonhero_fallback=nonhero_fallback,
                requested_hero=requested_hero,
                forced_hero=requested_hero and nonhero_fallback is None,
            )
        )

    fps = max(1, int(caption_windows.get("fps") or 30))
    selected = _select_feasible_candidates(candidates, fps=fps)
    forced_heroes = sum(1 for candidate in selected if candidate.forced_hero)
    optional_heroes = sorted(
        [
            candidate
            for candidate in selected
            if candidate.requested_hero and not candidate.forced_hero
        ],
        key=lambda candidate: (-candidate.choice.priority, candidate.choice.event_id),
    )
    keep_optional_hero_ids = {
        candidate.choice.event_id for candidate in optional_heroes[: max(0, 2 - forced_heroes)]
    }
    hero_downgraded_event_ids: list[str] = []
    final_choices: list[PostProcessCaptionChoice] = []
    for candidate in selected:
        choice = candidate.choice
        if (
            candidate.requested_hero
            and not candidate.forced_hero
            and choice.event_id not in keep_optional_hero_ids
            and candidate.nonhero_fallback is not None
        ):
            choice = PostProcessCaptionChoice(
                event_id=choice.event_id,
                caption_option_id=_as_str(candidate.nonhero_fallback.get("caption_option_id")),
                priority=choice.priority,
                reason=(choice.reason + "；hero 超限已回退 emphasis").strip("；"),
            )
            hero_downgraded_event_ids.append(choice.event_id)
        final_choices.append(choice)

    final_choices.sort(
        key=lambda choice: (
            int(windows_by_id[choice.event_id].get("start_frame") or 0),
            int(windows_by_id[choice.event_id].get("end_frame") or 0),
            choice.event_id,
        )
    )
    selected_ids = {choice.event_id for choice in final_choices}
    pruned_event_ids = [
        _as_str(window.get("event_id"))
        for window in sorted(
            selectable_windows,
            key=lambda item: (
                int(item.get("start_frame") or 0),
                int(item.get("end_frame") or 0),
                _as_str(item.get("event_id")),
            ),
        )
        if _as_str(window.get("event_id")) not in selected_ids
    ]
    max_feasible_count = max_feasible_emphasis_count(selectable_windows, fps=fps)
    diagnostics = {
        "max_feasible_count": max_feasible_count,
        "target_count": min(_CAPTION_MAX_EVENTS, max_feasible_count),
        "selected_count": len(final_choices),
        "pruned_event_ids": pruned_event_ids,
        "defaulted_option_event_ids": sorted(set(defaulted_option_event_ids)),
        "hero_downgraded_event_ids": sorted(hero_downgraded_event_ids),
        "unknown_event_ids": sorted(set(unknown_event_ids)),
        "invalid_bgm_id": invalid_bgm_id,
        "used_deterministic_fallback": bool(
            deterministic_fallback
            or defaulted_option_event_ids
            or hero_downgraded_event_ids
            or unknown_event_ids
            or invalid_bgm_id
        ),
    }
    return (
        PostProcessSelection(
            bgm_id=bgm_id,
            caption_choices=final_choices,
            analysis=selection.analysis,
        ),
        diagnostics,
    )


def _select_feasible_candidates(
    candidates: list[_SolverCandidate], *, fps: int
) -> tuple[_SolverCandidate, ...]:
    """Weighted interval DP: cardinality first, semantic priority second."""

    ordered = sorted(
        candidates,
        key=lambda candidate: (
            int(candidate.window.get("end_frame") or 0),
            int(candidate.window.get("start_frame") or 0),
            candidate.choice.event_id,
        ),
    )
    gap_frames = int(math.ceil(EMPHASIS_EVENT_GAP_SEC * max(1, fps)))
    previous_indices: list[int] = []
    for index, candidate in enumerate(ordered):
        start = int(candidate.window.get("start_frame") or 0)
        previous = -1
        for other_index in range(index - 1, -1, -1):
            other_end = int(ordered[other_index].window.get("end_frame") or 0)
            if start - other_end >= gap_frames:
                previous = other_index
                break
        previous_indices.append(previous)

    # dp[i] contains the best subsets using the first i ordered candidates, keyed
    # by (event_count, forced_hero_count). Optional heroes can be downgraded later.
    dp: list[dict[tuple[int, int], tuple[_SolverCandidate, ...]]] = [{(0, 0): ()}]
    for index, candidate in enumerate(ordered):
        current = dict(dp[index])
        for (count, hero_count), subset in dp[previous_indices[index] + 1].items():
            next_count = count + 1
            next_hero_count = hero_count + int(candidate.forced_hero)
            if next_count > _CAPTION_MAX_EVENTS or next_hero_count > 2:
                continue
            key = (next_count, next_hero_count)
            proposed = (*subset, candidate)
            if key not in current or _prefer_candidate_subset(proposed, current[key]):
                current[key] = proposed
        dp.append(current)

    max_count = max((key[0] for key in dp[-1]), default=0)
    finalists = [subset for key, subset in dp[-1].items() if key[0] == max_count]
    best: tuple[_SolverCandidate, ...] = ()
    for subset in finalists:
        if not best or _prefer_candidate_subset(subset, best):
            best = subset
    return best


def _prefer_candidate_subset(
    proposed: tuple[_SolverCandidate, ...], current: tuple[_SolverCandidate, ...]
) -> bool:
    proposed_priority = sum(item.choice.priority for item in proposed)
    current_priority = sum(item.choice.priority for item in current)
    if proposed_priority != current_priority:
        return proposed_priority > current_priority
    proposed_ids = tuple(sorted(item.choice.event_id for item in proposed))
    current_ids = tuple(sorted(item.choice.event_id for item in current))
    return proposed_ids < current_ids


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
