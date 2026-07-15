"""Pure helpers for the frame-authoritative CaptionWindow plan."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable

from packages.core.contracts.artifacts import EmphasisHint
from packages.production.pipeline._caption_display import compile_caption_display
from packages.production.pipeline._caption_effects import effect_envelope
from packages.production.pipeline._caption_visual_presets import caption_visual_preset
from packages.production.pipeline._huazi_candidates import derive_huazi_candidates
from packages.production.pipeline._huazi_layout import generate_layout_boxes

_MAX_OPTIONS_PER_EVENT = 24
_MAX_SAFE_ANCHORS_PER_EVENT = 6

# Emphasis captions cut in on the exact spoken beat but must linger long enough to
# read. Each candidate is independently held to the segment boundary; conflicts are
# then made explicit and resolved after semantic ranking by the local solver.
EMPHASIS_MIN_HOLD_SEC = 1.2
EMPHASIS_EVENT_GAP_SEC = 0.8
NORMAL_CAPTION_GAP_FRAMES = 2

# Reference-grounded negative-space anchors for normal captions. The geometry is
# still finalized from the actual measured text box and proved on the final
# composite; these are only deterministic search seeds, never provider output.
_NORMAL_POSITION_SEEDS = (
    ("left_upper", "left", 0.11, 0.30),
    ("right_upper", "right", 0.89, 0.30),
    ("left_middle", "left", 0.18, 0.42),
    ("right_middle", "right", 0.89, 0.42),
    ("left_lower", "left", 0.18, 0.56),
    ("right_lower", "right", 0.89, 0.56),
    ("center_upper", "center", 0.50, 0.34),
    ("center_middle", "center", 0.50, 0.46),
    ("center_lower", "center", 0.50, 0.58),
)


def normalize_caption_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char.lower() for char in normalized if not char.isspace())


def frame_index_at_fps(seconds: float, fps: int) -> int:
    return max(0, int(math.floor(float(seconds) * max(1, int(fps)) + 0.5)))


def compile_normal_windows(
    *,
    units: list[dict],
    resolution: tuple[int, int],
    fps: int,
    total_frames: int,
    margin_l: int,
    margin_r: int,
    measure: Callable[[str], float],
    metrics_source: str,
    enabled: bool,
    tokens: list[dict] | None = None,
    cut_frames: set[int] | None = None,
    max_lines: int = 2,
    max_line_width_px: float | None = None,
) -> tuple[list[dict], dict]:
    """Compile normal captions with single-owner tokens and separate display spans."""

    result = compile_caption_display(
        units=units,
        resolution=resolution,
        margin_l=margin_l,
        margin_r=margin_r,
        measure=measure,
        metrics_source=metrics_source,
        normal_enabled=enabled,
        emphasis_enabled=False,
        overlay_events=[],
        max_lines=max_lines,
        max_line_width_px=max_line_width_px,
    )
    windows: list[dict] = []
    token_matched = 0
    char_fallback = 0
    token_claim_failures = 0
    tokens = [
        item
        for item in (tokens or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    token_cursor = 0
    cut_frames = cut_frames or set()
    for index, cue in enumerate(result.normal_cues):
        base_start_frame = min(total_frames, frame_index_at_fps(cue.start, fps))
        base_end_frame = min(total_frames, frame_index_at_fps(cue.end, fps))
        if base_end_frame <= base_start_frame:
            continue
        fallback_source_ids = [
            str(units[item].get("unit_id") or f"unit_{item + 1:03d}")
            for item in cue.source_unit_ids
            if isinstance(item, int) and 0 <= item < len(units)
        ]
        text = "".join(cue.lines)
        claim = _claim_contiguous_tokens(tokens, token_cursor, text)
        cue_tokens: list[dict] = []
        if claim is not None:
            claim_start, claim_end = claim
            cue_tokens = tokens[claim_start:claim_end]
            token_cursor = claim_end
        elif tokens:
            token_claim_failures += 1

        if cue_tokens:
            spoken_start_frame = min(
                total_frames,
                frame_index_at_fps(float(cue_tokens[0].get("start") or 0.0), fps),
            )
            spoken_end_frame = min(
                total_frames,
                frame_index_at_fps(float(cue_tokens[-1].get("end") or 0.0), fps),
            )
            if spoken_end_frame <= spoken_start_frame:
                spoken_end_frame = min(total_frames, spoken_start_frame + 1)
            token_matched += len(cue_tokens)
        else:
            char_fallback += 1
            spoken_start_frame = base_start_frame
            spoken_end_frame = base_end_frame

        display_start_frame = spoken_start_frame
        display_end_frame = max(spoken_end_frame, base_end_frame)
        future_cuts = sorted(
            cut for cut in cut_frames if spoken_end_frame <= cut < display_end_frame
        )
        if future_cuts:
            display_end_frame = future_cuts[0]
        if display_end_frame <= display_start_frame:
            display_end_frame = min(total_frames, display_start_frame + 1)
        if display_end_frame <= display_start_frame:
            continue

        owned_source_ids = list(
            dict.fromkeys(
                str(item.get("source_unit_id")) for item in cue_tokens if item.get("source_unit_id")
            )
        )
        source_ids = owned_source_ids or fallback_source_ids
        token_ids = [str(item.get("token_id")) for item in cue_tokens if item.get("token_id")]
        char_spans = [
            _coerce_char_span(item.get("char_span"))
            for item in cue_tokens
            if _coerce_char_span(item.get("char_span")) is not None
        ]
        char_span = [char_spans[0][0], char_spans[-1][1]] if char_spans else None
        line_start_frames = _line_start_frames(
            lines=list(cue.lines),
            cue_tokens=cue_tokens,
            cue_start_frame=display_start_frame,
            fps=fps,
        )
        windows.append(
            {
                "window_id": f"caption_{index + 1:03d}",
                "start_frame": display_start_frame,
                "end_frame": display_end_frame,
                "spoken_span": {
                    "start_frame": spoken_start_frame,
                    "end_frame": spoken_end_frame,
                },
                "display_span": {
                    "start_frame": display_start_frame,
                    "end_frame": display_end_frame,
                },
                "token_ids": token_ids,
                "char_span": char_span,
                "lines": list(cue.lines),
                "line_start_frames": line_start_frames,
                "source_unit_ids": source_ids,
                "normalized_text": normalize_caption_text(text),
                "visual_preset_id": "normal",
                "effect_id": (
                    "none"
                    if any(abs(display_start_frame - cut) <= 1 for cut in cut_frames)
                    else "soft_in"
                ),
            }
        )
    caption_gap_clamps = _clamp_normal_display_spans(windows)
    return windows, {
        "merged_units": result.diagnostics.merged_units,
        "split_cues": result.diagnostics.split_cues,
        "font_metrics_source": result.diagnostics.font_metrics_source,
        "token_matched": token_matched,
        "char_fallback": char_fallback,
        "token_claim_failures": token_claim_failures,
        "caption_gap_clamps": caption_gap_clamps,
    }


def _claim_contiguous_tokens(
    tokens: list[dict], start_index: int, text: str
) -> tuple[int, int] | None:
    """Claim one exact script-text token run; claimed tokens are never revisited."""

    needle = _normalize_ownership_text(text)
    if not needle:
        return None
    for candidate_start in range(start_index, len(tokens)):
        value = ""
        matched_end: int | None = None
        for end_index in range(candidate_start, len(tokens)):
            piece = _normalize_ownership_text(str(tokens[end_index].get("text") or ""))
            if matched_end is not None:
                if piece:
                    return candidate_start, matched_end
                matched_end = end_index + 1
                continue
            value += piece
            if value == needle:
                matched_end = end_index + 1
                continue
            if len(value) >= len(needle) or not needle.startswith(value):
                break
        if matched_end is not None:
            return candidate_start, matched_end
    return None


def _normalize_ownership_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char.lower() for char in normalized if char.isalnum())


def _coerce_char_span(value: object) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        start, end = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    return (start, end) if start >= 0 and end > start else None


def _clamp_normal_display_spans(windows: list[dict]) -> int:
    """Guarantee a two-frame display gap without mutating spoken truth."""

    clamps = 0
    for previous, current in zip(windows, windows[1:]):
        allowed_previous_end = int(current["start_frame"]) - NORMAL_CAPTION_GAP_FRAMES
        if int(previous["end_frame"]) <= allowed_previous_end:
            continue
        if allowed_previous_end <= int(previous["start_frame"]):
            shifted_start = int(previous["start_frame"]) + NORMAL_CAPTION_GAP_FRAMES + 1
            if shifted_start < int(current["end_frame"]):
                current["start_frame"] = shifted_start
                current["display_span"]["start_frame"] = shifted_start
                current["line_start_frames"] = [
                    max(shifted_start, int(frame))
                    for frame in current.get("line_start_frames") or []
                ]
                allowed_previous_end = shifted_start - NORMAL_CAPTION_GAP_FRAMES
        new_end = max(int(previous["start_frame"]) + 1, allowed_previous_end)
        if new_end < int(previous["end_frame"]):
            previous["end_frame"] = new_end
            previous["display_span"]["end_frame"] = new_end
            previous["line_start_frames"] = [
                min(max(int(previous["start_frame"]), int(frame)), new_end - 1)
                for frame in previous.get("line_start_frames") or []
            ]
            clamps += 1
    return clamps


def normal_safe_rect(
    *,
    width: int,
    position_y: float,
    top_y: float,
    margin_l: int,
    margin_r: int,
) -> dict:
    safe_width = max(1, width - margin_l - margin_r)
    top = max(0.0, min(1.0, float(top_y)))
    bottom = max(top, min(1.0, float(position_y)))
    return {
        "x": round(max(0, margin_l) / max(1, width), 4),
        "y": round(top, 4),
        "w": round(safe_width / max(1, width), 4),
        "h": round(bottom - top, 4),
    }


def build_normal_caption_position_candidates(
    *,
    window_id: str,
    lines: list[str],
    width: int,
    height: int,
    measure: Callable[[str], float],
    font_size: float,
    outline: float,
    shadow_x: float,
    shadow_y: float,
    requested_position_y: float,
    vertical_shift_px: float = 0.0,
) -> list[dict]:
    """Build measured normal-caption boxes for deterministic pixel analysis."""

    canvas_width = max(1, int(width))
    canvas_height = max(1, int(height))
    clean_lines = [str(line).strip() for line in lines if str(line).strip()]
    if not clean_lines:
        return []
    border = max(0.0, float(outline))
    box_width_px = (
        max(max(1.0, float(measure(line))) for line in clean_lines) * 1.04
        + border * 2.0
        + max(0.0, float(shadow_x))
    )
    box_height_px = (
        max(1.0, float(font_size)) * 1.12 * len(clean_lines)
        + border * 2.0
        + max(0.0, float(shadow_y))
    )
    box_width = box_width_px / canvas_width
    box_height = box_height_px / canvas_height
    shift = max(0.0, float(vertical_shift_px)) / canvas_height
    # This is a safety envelope, not a layout preference. Clamping it would
    # make the detector inspect fewer pixels than libass eventually burns
    # (notably for 96px three-line captions and unbreakable Latin tokens).
    # Reject geometry that cannot fit inside the 4%/8% title-safe canvas.
    if (
        box_width <= 0.0
        or box_height <= 0.0
        or box_width > 0.92
        or box_height + shift > 0.88
    ):
        return []

    seeds = list(_NORMAL_POSITION_SEEDS)
    fallback_top = max(0.05, min(0.92 - box_height, float(requested_position_y) - box_height))
    seeds.append(("requested_bottom", "center", 0.50, fallback_top))
    options: list[dict] = []
    for rank, (anchor_id, text_align, anchor_x, top_y) in enumerate(seeds):
        if text_align == "left":
            left = anchor_x
        elif text_align == "right":
            left = anchor_x - box_width
        else:
            left = anchor_x - box_width / 2.0
        left = max(0.04, min(0.96 - box_width, left))
        top = max(0.04, min(0.92 - box_height, top_y))
        rect = {
            "x": round(left, 4),
            "y": round(top, 4),
            "w": round(box_width, 4),
            "h": round(box_height, 4),
        }
        envelope_bottom = rect["y"] + rect["h"] + shift
        if envelope_bottom > 1.0:
            continue
        safety_envelope = {
            **rect,
            "h": round(envelope_bottom - rect["y"], 4),
        }
        options.append(
            {
                "caption_option_id": f"{window_id}__{anchor_id}",
                "anchor_id": anchor_id,
                "rect": rect,
                "text_align": text_align,
                "position_rank": rank,
                "safety_envelope": safety_envelope,
            }
        )
    return options


def timeline_cut_frames(timeline: dict, total_frames: int) -> set[int]:
    cuts: set[int] = {0}
    for track in timeline.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        for key in ("timeline_start_frame", "timeline_end_frame"):
            value = track.get(key)
            if value is None:
                continue
            frame = int(value)
            if 0 < frame < total_frames:
                cuts.add(frame)
    return cuts


def build_emphasis_windows(
    *,
    emphasis: list[EmphasisHint],
    units: list[dict],
    fps: int,
    total_frames: int,
    cut_frames: set[int],
    resolution: tuple[int, int],
    normal_caption_top_y: float,
    tokens: list[dict] | None = None,
) -> tuple[list[dict], int, int, int, int, int, int]:
    """Derive fixed emphasis windows and static geometry candidates.

    Phrase position is estimated deterministically from its character offset in the
    narration unit.  The resulting short window is clamped into the final visual
    segment containing that center, so it never crosses a portrait or B-roll cut.
    A second pass holds each window's tail to ``EMPHASIS_MIN_HOLD_SEC`` where the
    containing visual segment allows it (the cut-in beat never moves). Candidate
    neighbours never shorten one another; downstream receives an explicit conflict
    graph and chooses a maximum legal subset.
    """

    candidate_events = derive_huazi_candidates(emphasis, units)
    windows: list[dict] = []
    crossing_cuts = 0
    token_matched = 0
    char_fallback = 0
    for event in candidate_events:
        unit_match = _source_unit_for_event(
            event_start=event.start,
            event_end=event.end,
            event_text=event.text,
            units=units,
        )
        if unit_match is None:
            continue
        unit_index, unit = unit_match
        fixed_window = _phrase_window(
            phrase=event.text,
            unit=unit,
            fps=fps,
            total_frames=total_frames,
            cut_frames=cut_frames,
            tokens=tokens or [],
        )
        if fixed_window is None:
            crossing_cuts += 1
            continue
        start_frame, end_frame, timing_source = fixed_window
        if timing_source == "token_matched":
            token_matched += 1
        else:
            char_fallback += 1
        source_ids = [str(unit.get("unit_id") or f"unit_{unit_index + 1:03d}")]
        raw_boxes = generate_layout_boxes(
            event_text=event.text,
            resolution=resolution,
            normal_caption_top_y=normal_caption_top_y,
            neighbor_boxes=[],
        )
        anchors = []
        for box in raw_boxes:
            anchor_id = f"{event.event_id}__{box['layout_box_id']}"
            anchors.append({**box, "anchor_id": anchor_id})
        windows.append(
            {
                "event_id": str(event.event_id or ""),
                "text": event.text,
                "normalized_text": normalize_caption_text(event.text),
                "start_frame": start_frame,
                "end_frame": end_frame,
                "source_unit_ids": source_ids,
                "anchor_candidates": anchors,
                "caption_options": [],
                "hero_eligible": any(abs(start_frame - cut) <= 1 for cut in cut_frames),
            }
        )
    hold_extended, hold_below_min = _extend_emphasis_hold(
        windows, fps=fps, total_frames=total_frames, cut_frames=cut_frames
    )
    return (
        windows,
        len(candidate_events),
        crossing_cuts,
        token_matched,
        char_fallback,
        hold_extended,
        hold_below_min,
    )


def build_caption_option_candidates(
    *,
    event_id: str,
    text: str,
    anchors: list[dict],
    width: int,
    height: int,
    measure: Callable[[str], float],
    font_size: float,
    outline: float,
    shadow: float,
    normal_safe_rect: dict | None,
    normal_safe_rects: list[dict] | None = None,
    hero_eligible: bool = False,
) -> list[dict]:
    """Build animation-specific options with their actual render envelopes.

    Text width comes from the effective emphasis font's hmtx measurer (or the
    explicit EAW fallback). The bbox includes the ASS cell height, bolding
    headroom, outline and shadow. Animation scale/translation then expands it to
    the exact conservative envelope that pixel safety must prove.
    """

    canvas_width = max(1, int(width))
    canvas_height = max(1, int(height))
    border = max(0.0, float(outline)) + max(0.0, float(shadow))
    text_width = max(1.0, float(measure(str(text)))) * 1.04 + border * 2.0
    text_height = max(1.0, float(font_size)) + border * 2.0
    options: list[dict] = []
    for anchor in anchors:
        anchor_id = str(anchor.get("anchor_id") or "")
        preset_ids = ["emphasis"]
        if hero_eligible:
            preset_ids.append("hero")
        for preset_id in preset_ids:
            preset = caption_visual_preset(preset_id)
            animation_id = preset.effect_id
            scale, vertical_shift = effect_envelope(animation_id)
            endpoint = _anchored_bbox_px(
                anchor=anchor,
                text_width=text_width * preset.size_ratio * scale,
                text_height=text_height * preset.size_ratio * scale,
                canvas_width=canvas_width,
                canvas_height=canvas_height,
            )
            envelope = _bbox_union(
                endpoint,
                _translate_bbox(endpoint, 0.0, vertical_shift),
            )
            if not _bbox_inside_canvas(envelope, canvas_width, canvas_height):
                continue
            normalized_envelope = _normalize_bbox(envelope, canvas_width, canvas_height)
            blocked_normal_rects = [
                rect
                for rect in [normal_safe_rect, *(normal_safe_rects or [])]
                if isinstance(rect, dict)
            ]
            if any(_rects_overlap(normalized_envelope, rect) for rect in blocked_normal_rects):
                continue
            option_id = f"{event_id}__{anchor_id}__{preset_id}"
            options.append(
                {
                    "caption_option_id": option_id,
                    "anchor_id": anchor_id,
                    "typography_variant_id": f"{preset_id}_v1",
                    "animation_id": animation_id,
                    "visual_preset_id": preset_id,
                    "safety_envelope": normalized_envelope,
                }
            )
    return options


def finalize_safe_caption_options(
    *,
    anchors: list[dict],
    safe_options: list[dict],
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Apply deterministic caps and persist only anchors/options proven safe."""

    safe_anchor_ids = {str(option.get("anchor_id") or "") for option in safe_options}
    ordered_safe_anchor_ids = [
        str(anchor.get("anchor_id") or "")
        for anchor in anchors
        if str(anchor.get("anchor_id") or "") in safe_anchor_ids
    ]
    kept_anchor_ids = set(ordered_safe_anchor_ids[:_MAX_SAFE_ANCHORS_PER_EVENT])
    options_after_anchor_cap = [
        option for option in safe_options if str(option.get("anchor_id") or "") in kept_anchor_ids
    ]
    final_options_internal = options_after_anchor_cap[:_MAX_OPTIONS_PER_EVENT]
    final_anchor_ids = {str(option.get("anchor_id") or "") for option in final_options_internal}
    persisted_options = [
        {
            "caption_option_id": option["caption_option_id"],
            "anchor_id": option["anchor_id"],
            "typography_variant_id": option["typography_variant_id"],
            "animation_id": option["animation_id"],
            "visual_preset_id": option.get("visual_preset_id", "emphasis"),
        }
        for option in final_options_internal
    ]
    persisted_anchors: list[dict] = []
    for anchor in anchors:
        anchor_id = str(anchor.get("anchor_id") or "")
        if anchor_id not in final_anchor_ids:
            continue
        anchor_options = [
            option
            for option in final_options_internal
            if str(option.get("anchor_id") or "") == anchor_id
        ]
        persisted_anchors.append(
            {
                "anchor_id": anchor_id,
                "rect": anchor["rect"],
                "text_align": anchor["text_align"],
                "allowed_animation_ids": [
                    animation_id
                    for animation_id in ("pop", "slam_scale")
                    if any(option.get("animation_id") == animation_id for option in anchor_options)
                ],
                "region_tags": list(anchor.get("region_tags") or []),
                "face_overlap": round(
                    max((float(item.get("face_overlap") or 0.0) for item in anchor_options)),
                    4,
                ),
                "scene_text_overlap": round(
                    max((float(item.get("scene_text_overlap") or 0.0) for item in anchor_options)),
                    4,
                ),
                "busy_score": round(
                    max((float(item.get("busy_score") or 0.0) for item in anchor_options)),
                    4,
                ),
                "sample_frames": list(anchor_options[0].get("sample_frames") or []),
            }
        )
    return (
        persisted_anchors,
        persisted_options,
        {
            "safe_anchor_candidates": len(ordered_safe_anchor_ids),
            "anchors_pruned_by_cap": max(
                0, len(ordered_safe_anchor_ids) - _MAX_SAFE_ANCHORS_PER_EVENT
            ),
            "options_pruned_by_cap": max(0, len(safe_options) - len(persisted_options)),
        },
    )


def _anchored_bbox_px(
    *,
    anchor: dict,
    text_width: float,
    text_height: float,
    canvas_width: int,
    canvas_height: int,
) -> tuple[float, float, float, float]:
    rect = anchor.get("rect") or {}
    x = float(rect.get("x") or 0.0)
    y = float(rect.get("y") or 0.0)
    box_width = float(rect.get("w") or 0.0)
    box_height = float(rect.get("h") or 0.0)
    anchor_y = (y + box_height / 2.0) * canvas_height
    align = str(anchor.get("text_align") or "center")
    if align == "left":
        left = x * canvas_width
    elif align == "right":
        left = (x + box_width) * canvas_width - text_width
    else:
        left = (x + box_width / 2.0) * canvas_width - text_width / 2.0
    return left, anchor_y - text_height / 2.0, text_width, text_height


def _translate_bbox(
    bbox: tuple[float, float, float, float], dx: float, dy: float
) -> tuple[float, float, float, float]:
    return bbox[0] + dx, bbox[1] + dy, bbox[2], bbox[3]


def _bbox_union(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    left = min(first[0], second[0])
    top = min(first[1], second[1])
    right = max(first[0] + first[2], second[0] + second[2])
    bottom = max(first[1] + first[3], second[1] + second[3])
    return left, top, right - left, bottom - top


def _bbox_inside_canvas(bbox: tuple[float, float, float, float], width: int, height: int) -> bool:
    x, y, box_width, box_height = bbox
    return (
        x >= 0.0
        and y >= 0.0
        and box_width > 0.0
        and box_height > 0.0
        and x + box_width <= width
        and y + box_height <= height
    )


def _normalize_bbox(
    bbox: tuple[float, float, float, float], width: int, height: int
) -> dict[str, float]:
    return {
        "x": round(bbox[0] / width, 6),
        "y": round(bbox[1] / height, 6),
        "w": round(bbox[2] / width, 6),
        "h": round(bbox[3] / height, 6),
    }


def _rects_overlap(first: dict, second: dict) -> bool:
    left = max(float(first.get("x") or 0.0), float(second.get("x") or 0.0))
    top = max(float(first.get("y") or 0.0), float(second.get("y") or 0.0))
    right = min(
        float(first.get("x") or 0.0) + float(first.get("w") or 0.0),
        float(second.get("x") or 0.0) + float(second.get("w") or 0.0),
    )
    bottom = min(
        float(first.get("y") or 0.0) + float(first.get("h") or 0.0),
        float(second.get("y") or 0.0) + float(second.get("h") or 0.0),
    )
    return right > left and bottom > top


def _source_unit_for_event(
    *, event_start: float, event_end: float, event_text: str, units: list[dict]
) -> tuple[int, dict] | None:
    needle = normalize_caption_text(event_text)
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        start = float(unit.get("start") or 0.0)
        end = float(unit.get("end") or 0.0)
        if (
            abs(start - float(event_start)) < 1e-6
            and abs(end - float(event_end)) < 1e-6
            and needle in normalize_caption_text(str(unit.get("text") or ""))
        ):
            return index, unit
    return None


def _phrase_window(
    *,
    phrase: str,
    unit: dict,
    fps: int,
    total_frames: int,
    cut_frames: set[int],
    tokens: list[dict],
) -> tuple[int, int, str] | None:
    unit_start = min(total_frames, frame_index_at_fps(float(unit.get("start") or 0.0), fps))
    unit_end = min(total_frames, frame_index_at_fps(float(unit.get("end") or 0.0), fps))
    if unit_end <= unit_start:
        return None
    source_unit_id = str(unit.get("unit_id") or "")
    unit_tokens = [
        item
        for item in tokens
        if source_unit_id and str(item.get("source_unit_id") or "") == source_unit_id
    ]
    if not unit_tokens:
        # Historical AlignmentArtifact.v1 rows have no source_unit_id. This read-only
        # compatibility path is limited to emphasis phrase lookup; normal captions
        # never use temporal overlap to claim text.
        unit_tokens = _tokens_in_window(
            tokens,
            float(unit.get("start") or 0.0),
            float(unit.get("end") or 0.0),
        )
    token_span = _match_token_span(phrase, unit_tokens)
    if token_span is not None:
        start = min(total_frames, frame_index_at_fps(token_span[0], fps))
        end = min(total_frames, frame_index_at_fps(token_span[1], fps))
        if end <= start:
            end = min(total_frames, start + 1)
        if end <= start or any(start < cut < end for cut in cut_frames):
            return None
        return start, end, "token_matched"
    haystack = normalize_caption_text(str(unit.get("text") or ""))
    needle = normalize_caption_text(phrase)
    offset = haystack.find(needle)
    if not haystack or not needle or offset < 0:
        return None
    center_ratio = (offset + len(needle) / 2.0) / len(haystack)
    center = unit_start + int(round((unit_end - unit_start - 1) * center_ratio))

    segment_start, segment_end = _segment_bounds(center, cut_frames, total_frames)
    available_start = max(unit_start, segment_start)
    available_end = min(unit_end, segment_end)
    min_frames = max(3, int(math.ceil(0.3 * fps)))
    if available_end - available_start < min_frames:
        return None

    phrase_share_frames = int(
        math.ceil((len(needle) / max(1, len(haystack))) * (unit_end - unit_start))
    )
    desired = max(min_frames, phrase_share_frames)
    desired = min(desired, available_end - available_start)
    center = max(available_start, min(available_end - 1, center))
    start = center - desired // 2
    start = max(available_start, min(start, available_end - desired))
    end = start + desired
    if any(start < cut < end for cut in cut_frames):
        return None
    return start, end, "char_fallback"


def _segment_bounds(frame: int, cut_frames: set[int], total_frames: int) -> tuple[int, int]:
    """Return the ``[start, end)`` visual segment containing ``frame``.

    Segment boundaries are the timeline cuts, so a window kept inside one segment
    never crosses a portrait or B-roll cut.
    """
    boundaries = [0, *sorted(cut for cut in cut_frames if 0 < cut < total_frames), total_frames]
    for left, right in zip(boundaries, boundaries[1:], strict=True):
        if left <= frame < right:
            return left, right
    return 0, total_frames


def emphasis_conflict_graph(windows: list[dict], *, fps: int) -> list[dict]:
    """Return every pair that violates overlap/gap legality before the LLM runs."""

    required_gap = int(math.ceil(EMPHASIS_EVENT_GAP_SEC * max(1, fps)))
    selectable = sorted(
        [window for window in windows if window.get("caption_options")],
        key=lambda window: (
            int(window.get("start_frame") or 0),
            int(window.get("end_frame") or 0),
            str(window.get("event_id") or ""),
        ),
    )
    conflicts: list[dict] = []
    for index, first in enumerate(selectable):
        first_end = int(first.get("end_frame") or 0)
        for second in selectable[index + 1 :]:
            second_start = int(second.get("start_frame") or 0)
            actual_gap = second_start - first_end
            if actual_gap >= required_gap:
                break
            conflicts.append(
                {
                    "first_event_id": str(first.get("event_id") or ""),
                    "second_event_id": str(second.get("event_id") or ""),
                    "actual_gap_frames": actual_gap,
                    "required_gap_frames": required_gap,
                    "reason": "overlap" if actual_gap < 0 else "insufficient_gap",
                }
            )
    return conflicts


def max_feasible_emphasis_count(windows: list[dict], *, fps: int) -> int:
    """Maximum-cardinality interval subset under the local emphasis gap policy."""

    required_gap = int(math.ceil(EMPHASIS_EVENT_GAP_SEC * max(1, fps)))
    selectable = sorted(
        [window for window in windows if window.get("caption_options")],
        key=lambda window: (
            int(window.get("end_frame") or 0),
            int(window.get("start_frame") or 0),
            str(window.get("event_id") or ""),
        ),
    )
    count = 0
    previous_end: int | None = None
    for window in selectable:
        start = int(window.get("start_frame") or 0)
        if previous_end is not None and start - previous_end < required_gap:
            continue
        count += 1
        previous_end = int(window.get("end_frame") or 0)
    return count


def _extend_emphasis_hold(
    windows: list[dict],
    *,
    fps: int,
    total_frames: int,
    cut_frames: set[int],
) -> tuple[int, int]:
    """Hold each emphasis window's tail to a minimum on-screen duration.

    ``start_frame`` (the cut-in beat) never moves; only the tail extends, clamped to
    the containing segment and timeline end. Other candidates do not constrain the
    hold: they are not selected yet, so using them as ceilings would let an event
    that is later pruned make a surviving caption unreadably short.
    """
    if not windows:
        return 0, 0
    min_hold_frames = int(math.ceil(EMPHASIS_MIN_HOLD_SEC * fps))
    ordered = sorted(windows, key=lambda window: (window["start_frame"], window["end_frame"]))
    hold_extended = 0
    hold_below_min = 0
    for window in ordered:
        start_frame = window["start_frame"]
        original_end = window["end_frame"]
        _segment_start, segment_end = _segment_bounds(start_frame, cut_frames, total_frames)
        ceiling = min(segment_end, total_frames)
        target_end = min(start_frame + min_hold_frames, ceiling)
        end_final = max(original_end, target_end)
        window["end_frame"] = end_final
        if end_final > original_end:
            hold_extended += 1
        if end_final - start_frame < min_hold_frames:
            hold_below_min += 1
    return hold_extended, hold_below_min


def _tokens_in_window(tokens: list[dict], start: float, end: float) -> list[dict]:
    return sorted(
        [
            item
            for item in tokens
            if float(item.get("end") or 0.0) > start
            and float(item.get("start") or 0.0) < end
            and str(item.get("text") or "").strip()
        ],
        key=lambda item: (float(item.get("start") or 0.0), float(item.get("end") or 0.0)),
    )


def _match_token_span(phrase: str, tokens: list[dict]) -> tuple[float, float] | None:
    needle = normalize_caption_text(phrase)
    if not needle:
        return None
    pieces = [normalize_caption_text(str(item.get("text") or "")) for item in tokens]
    for start_index in range(len(tokens)):
        value = ""
        for end_index in range(start_index, len(tokens)):
            value += pieces[end_index]
            if value == needle:
                return (
                    float(tokens[start_index].get("start") or 0.0),
                    float(tokens[end_index].get("end") or 0.0),
                )
            if len(value) >= len(needle) or not needle.startswith(value):
                break
    return None


def _line_start_frames(
    *,
    lines: list[str],
    cue_tokens: list[dict],
    cue_start_frame: int,
    fps: int,
) -> list[int]:
    if len(lines) <= 1 or not cue_tokens:
        return [cue_start_frame for _line in lines]
    starts = [cue_start_frame]
    token_cursor = 0
    for line in lines:
        match = _match_token_span_from(line, cue_tokens, start_index=token_cursor)
        if match is not None:
            _start_time, _end_time, token_cursor = match
        if len(starts) >= len(lines):
            continue
        next_line = lines[len(starts)]
        next_match = _match_token_span_from(next_line, cue_tokens, start_index=token_cursor)
        if next_match is None:
            starts.append(cue_start_frame)
            continue
        next_start_time, _next_end_time, _next_cursor = next_match
        next_start = frame_index_at_fps(next_start_time, fps)
        if (next_start - cue_start_frame) / max(1, fps) < 0.3:
            next_start = cue_start_frame
        starts.append(next_start)
    return starts[: len(lines)]


def _match_token_span_from(
    phrase: str,
    tokens: list[dict],
    *,
    start_index: int,
) -> tuple[float, float, int] | None:
    needle = normalize_caption_text(phrase)
    if not needle:
        return None
    pieces = [normalize_caption_text(str(item.get("text") or "")) for item in tokens]
    for candidate_start in range(max(0, start_index), len(tokens)):
        value = ""
        for end_index in range(candidate_start, len(tokens)):
            value += pieces[end_index]
            if value == needle:
                return (
                    float(tokens[candidate_start].get("start") or 0.0),
                    float(tokens[end_index].get("end") or 0.0),
                    end_index + 1,
                )
            if len(value) >= len(needle) or not needle.startswith(value):
                break
    return None
