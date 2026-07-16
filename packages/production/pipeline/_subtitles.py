"""ASS authoring for the fixed-band caption-composition artifact."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

from packages.core.contracts.artifacts import (
    CaptionCompositionPlanArtifact,
    CaptionRun,
    StylePlanArtifact,
)
from packages.production.pipeline._caption_effects import (
    CaptionEffectRenderContext,
    caption_effect,
)
from packages.production.pipeline._fonts import is_ass_bold_weight
from packages.production.pipeline._emphasis_styles import emphasis_style

_ASS_MARGIN_L = 80
_ASS_MARGIN_R = 80


def ass_time(seconds: float) -> str:
    centiseconds = round(max(seconds, 0) * 100)
    hours, remainder = divmod(centiseconds, 3600 * 100)
    minutes, remainder = divmod(remainder, 60 * 100)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    cleaned = str(text).replace("{", "").replace("}", "")
    return cleaned.replace("\\", r"\{}").replace("\n", r"\N")


def write_ass_subtitles(
    output_path: Path,
    *,
    style: StylePlanArtifact,
    caption_composition: CaptionCompositionPlanArtifact,
    font_name: str,
    emphasis_font_name: str,
    font_weight: int = 400,
    emphasis_font_weight: int = 400,
    font_overrides: Mapping[str, tuple[str, int]] | None = None,
) -> list[str]:
    """Write registered effect fragments; line breaks and x positions are preplanned."""

    subtitle = style.subtitle
    resolved_font = font_name.replace(",", " ").strip()
    resolved_emphasis_font = emphasis_font_name.replace(",", " ").strip()
    if not resolved_font or not resolved_emphasis_font:
        raise ValueError("resolved caption font family names must not be empty")
    normal_size = caption_composition.normal_font_size
    emphasis_size = caption_composition.emphasis_font_size
    band = caption_composition.band
    width = caption_composition.width
    height = caption_composition.height
    anchor_x = band.anchor_x * width
    baseline_y = band.baseline_y * height
    line_height = max(normal_size, emphasis_size) * band.line_height_ratio
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding"
        ),
        _style_row(
            "Normal",
            resolved_font,
            normal_size,
            primary=subtitle.primary_color,
            outline_color=subtitle.outline_color,
            outline=subtitle.outline,
            font_weight=font_weight,
        ),
        _style_row(
            "Emphasis",
            resolved_emphasis_font,
            emphasis_size,
            primary=subtitle.emphasis_primary_color,
            outline_color=subtitle.emphasis_outline_color,
            outline=subtitle.emphasis_outline,
            font_weight=emphasis_font_weight,
        ),
    ]
    for style_id in _used_style_ids(caption_composition):
        spec = emphasis_style(style_id)
        planned_font_asset_id = planned_emphasis_font_asset_id(
            style_id,
            caption_composition,
        )
        family, weight = _resolved_style_font(
            planned_font_asset_id,
            composition=caption_composition,
            emphasis_font_name=resolved_emphasis_font,
            emphasis_font_weight=emphasis_font_weight,
            font_overrides=font_overrides,
        )
        lines.append(
            _style_row(
                f"Emph_{style_id}",
                family,
                _style_font_size(style_id, caption_composition),
                primary=caption_composition.emphasis_primary_color_override or spec.fill,
                outline_color=spec.outline,
                outline=spec.outline_width,
                font_weight=weight,
            )
        )
    lines.extend(
        (
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        )
    )
    fps = caption_composition.fps
    for cue in caption_composition.cues:
        cue_lines = cue.lines
        for line_index, line in enumerate(cue_lines):
            advance = line.advance_px
            first_font_id = _run_font_asset_id(line.runs[0], caption_composition)
            last_font_id = _run_font_asset_id(line.runs[-1], caption_composition)
            left_overhang = caption_composition.diagnostics.font_horizontal_left_overhang_px.get(
                first_font_id or "",
                0.0,
            )
            right_overhang = (
                caption_composition.diagnostics.font_horizontal_right_overhang_px.get(
                    last_font_id or "",
                    0.0,
                )
            )
            cursor_x = anchor_x - (
                advance + line.animation_headroom_px + right_overhang - left_overhang
            ) / 2.0
            baseline = baseline_y - (len(cue_lines) - line_index - 1) * line_height
            for run in line.runs:
                run_advance = run.advance_px
                effect = caption_effect(run.effect_id)
                left_headroom, right_headroom = effect.headroom_sides_px(run_advance)
                x = cursor_x + left_headroom
                role = (
                    f"Emph_{run.style_id}"
                    if run.role == "emphasis" and run.style_id
                    else "Emphasis"
                    if run.role == "emphasis"
                    else "Normal"
                )
                top_y = baseline - run.baseline_offset_px
                font_tags = _run_font_override_tags(
                    run,
                    caption_composition,
                    font_overrides,
                )
                fragments = effect.render(
                    CaptionEffectRenderContext(
                        text=run.text,
                        x=x,
                        y=top_y,
                        start_ms=round(run.enter_frame * 1000 / fps),
                        end_ms=round(run.exit_frame * 1000 / fps),
                        frame_duration_ms=1000 / fps,
                        char_enter_ms=(
                            tuple(round(frame * 1000 / fps) for frame in run.char_enter_frames)
                            if run.char_enter_frames is not None
                            else None
                        ),
                        char_advances_px=(
                            tuple(run.char_advances_px)
                            if run.char_advances_px is not None
                            else None
                        ),
                    )
                )
                backing = _backing_dialogue(
                    run,
                    x=x,
                    top_y=top_y,
                    start_ms=round(run.enter_frame * 1000 / fps),
                    end_ms=round(run.exit_frame * 1000 / fps),
                    composition=caption_composition,
                )
                if backing is not None:
                    lines.append(backing)
                for fragment in fragments:
                    if fragment.end_ms <= fragment.start_ms:
                        continue
                    lines.append(
                        "Dialogue: 1,"
                        f"{ass_time(fragment.start_ms / 1000)},"
                        f"{ass_time(fragment.end_ms / 1000)},{role},,0,0,0,,"
                        + "{" + "".join((*fragment.tags, *font_tags)) + "}"
                        + ass_escape(fragment.text)
                    )
                cursor_x += left_headroom + run_advance + right_headroom
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return []


def _run_font_override_tags(
    run: CaptionRun,
    composition: CaptionCompositionPlanArtifact,
    font_overrides: Mapping[str, tuple[str, int]] | None,
) -> tuple[str, ...]:
    planned_font_asset_id = (
        planned_emphasis_font_asset_id(run.style_id, composition)
        if run.role == "emphasis" and run.style_id
        else composition.emphasis_font_asset_id
        if run.role == "emphasis"
        else composition.normal_font_asset_id
    )
    if not run.font_asset_id or run.font_asset_id == planned_font_asset_id:
        return ()
    override = (font_overrides or {}).get(run.font_asset_id)
    if override is None:
        raise ValueError(f"caption run font override is unresolved: {run.font_asset_id}")
    override_name = override[0].replace(",", " ").strip()
    if not override_name or any(char in override_name for char in "{}\\"):
        raise ValueError("resolved caption run font family name is invalid")
    return (
        f"\\fn{override_name}",
        "\\b1" if is_ass_bold_weight(override[1]) else "\\b0",
    )


def _run_font_asset_id(
    run: CaptionRun,
    composition: CaptionCompositionPlanArtifact,
) -> str | None:
    if run.font_asset_id:
        return run.font_asset_id
    if run.role == "emphasis":
        return composition.emphasis_font_asset_id
    return composition.normal_font_asset_id


def _used_style_ids(composition: CaptionCompositionPlanArtifact) -> list[str]:
    return list(
        dict.fromkeys(
            run.style_id
            for cue in composition.cues
            for line in cue.lines
            for run in line.runs
            if run.role == "emphasis" and run.style_id
        )
    )


def planned_emphasis_font_asset_id(
    style_id: str,
    composition: CaptionCompositionPlanArtifact,
) -> str:
    requested = next(
        (
            run.requested_font_asset_id
            for cue in composition.cues
            for line in cue.lines
            for run in line.runs
            if run.style_id == style_id and run.requested_font_asset_id
        ),
        None,
    )
    if requested is not None:
        return requested
    return emphasis_style(style_id).font_asset_id


def _style_font_size(
    style_id: str,
    composition: CaptionCompositionPlanArtifact,
) -> int:
    return next(
        (
            run.font_size
            for cue in composition.cues
            for line in cue.lines
            for run in line.runs
            if run.style_id == style_id and run.font_size is not None
        ),
        max(12, round(composition.normal_font_size * emphasis_style(style_id).size_ratio)),
    )


def _resolved_style_font(
    font_asset_id: str,
    *,
    composition: CaptionCompositionPlanArtifact,
    emphasis_font_name: str,
    emphasis_font_weight: int,
    font_overrides: Mapping[str, tuple[str, int]] | None,
) -> tuple[str, int]:
    if font_asset_id == composition.emphasis_font_asset_id:
        return emphasis_font_name, emphasis_font_weight
    resolved = (font_overrides or {}).get(font_asset_id)
    if resolved is None:
        raise ValueError(f"emphasis style font is unresolved: {font_asset_id}")
    return resolved


def _backing_dialogue(
    run: CaptionRun,
    *,
    x: float,
    top_y: float,
    start_ms: int,
    end_ms: int,
    composition: CaptionCompositionPlanArtifact,
) -> str | None:
    if not run.style_id:
        return None
    spec = emphasis_style(run.style_id)
    if spec.backing is None or spec.backing_color is None:
        return None
    font_size = run.font_size or _style_font_size(run.style_id, composition)
    left = x
    top = top_y - 5.0
    right = x + run.advance_px
    bottom = top_y + font_size + 5.0
    animation = ""
    if spec.backing == "burst_star":
        center_x = (left + right) / 2.0
        center_y = (top + bottom) / 2.0
        outer_x = max(12.0, (right - left) / 2.0)
        outer_y = max(12.0, (bottom - top) / 2.0)
        points = []
        for index in range(24):
            angle = -math.pi / 2.0 + index * math.pi / 12.0
            radius = 1.0 if index % 2 == 0 else 0.72
            points.append(
                (
                    round(center_x + math.cos(angle) * outer_x * radius),
                    round(center_y + math.sin(angle) * outer_y * radius),
                )
            )
        drawing = "m " + " l ".join(f"{px} {py}" for px, py in points)
    elif spec.backing == "underline_swipe":
        top = top_y + font_size * 0.86
        bottom = top + max(5.0, font_size * 0.12)
        drawing = _rectangle_path(left, top, right, bottom)
        animation = (
            f"\\clip({round(left)},{round(top)},{round(left + 1)},{round(bottom)})"
            f"\\t(0,160,\\clip({round(left)},{round(top)},{round(right)},{round(bottom)}))"
        )
    else:
        drawing = _rectangle_path(left, top, right, bottom)
    tags = (
        "\\an7\\pos(0,0)\\p1\\bord0\\shad0"
        f"\\1c{_ass_color(spec.backing_color, '#FFFFFF')}\\1a&H00&{animation}"
    )
    return (
        "Dialogue: 0,"
        f"{ass_time(start_ms / 1000)},{ass_time(end_ms / 1000)},Normal,,0,0,0,,"
        + "{" + tags + "}" + drawing
    )


def _rectangle_path(left: float, top: float, right: float, bottom: float) -> str:
    return (
        f"m {round(left)} {round(top)} l {round(right)} {round(top)} "
        f"l {round(right)} {round(bottom)} l {round(left)} {round(bottom)}"
    )


def _style_row(
    name: str,
    font_name: str,
    font_size: int,
    *,
    primary: object,
    outline_color: object,
    outline: object,
    font_weight: int,
) -> str:
    bold = -1 if is_ass_bold_weight(font_weight) else 0
    return (
        f"Style: {name},{font_name},{font_size},{_ass_color(primary, '#FFFFFF')},"
        f"&H000000FF,{_ass_color(outline_color, '#000000')},&H64000000,"
        f"{bold},0,0,0,100,100,0,0,1,{_ass_outline(outline)},0,7,"
        f"{_ASS_MARGIN_L},{_ASS_MARGIN_R},0,1"
    )


def _ass_color(value: object, fallback: str) -> str:
    parsed = _parse_hex_rgb(value) or _parse_hex_rgb(fallback) or (255, 255, 255)
    red, green, blue = parsed
    return f"&H00{blue:02X}{green:02X}{red:02X}"


def _parse_hex_rgb(value: object) -> tuple[int, int, int] | None:
    text = str(value or "").strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _ass_outline(value: float | None, fallback: float = 4.0) -> str:
    return f"{max(0.0, value if value is not None else fallback):g}"


def ass_font_size(requested_size: int | None, *, height: int) -> int:
    base_size = requested_size or 64
    scale = max(1.0, height / 1080.0)
    return max(12, int(round(base_size * scale)))
