"""ASS authoring for the fixed-band caption-composition artifact."""

from __future__ import annotations

from pathlib import Path

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
    style: dict,
    width: int,
    height: int,
    caption_composition: dict,
    font_name: str | None = None,
    emphasis_font_name: str | None = None,
) -> list[str]:
    """Write one Dialogue per CaptionRun; line breaks and x positions are preplanned."""

    subtitle = style.get("subtitle") if isinstance(style.get("subtitle"), dict) else {}
    resolved_font = (font_name or "").replace(",", " ").strip() or "Arial"
    resolved_emphasis_font = (
        (emphasis_font_name or font_name or "").replace(",", " ").strip() or resolved_font
    )
    normal_size = int(caption_composition.get("normal_font_size") or 64)
    emphasis_size = int(caption_composition.get("emphasis_font_size") or normal_size)
    band = caption_composition.get("band") if isinstance(caption_composition.get("band"), dict) else {}
    anchor_x = float(band.get("anchor_x", 0.5)) * width
    baseline_y = float(band.get("baseline_y", 0.84)) * height
    line_height = max(normal_size, emphasis_size) * float(band.get("line_height_ratio", 1.12))
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
            primary=subtitle.get("primary_color"),
            outline_color=subtitle.get("outline_color"),
            outline=subtitle.get("outline"),
        ),
        _style_row(
            "Emphasis",
            resolved_emphasis_font,
            emphasis_size,
            primary=subtitle.get("emphasis_primary_color"),
            outline_color=subtitle.get("emphasis_outline_color"),
            outline=subtitle.get("emphasis_outline"),
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    fps = max(1, int(caption_composition.get("fps") or 30))
    for cue in caption_composition.get("cues") or []:
        cue_lines = cue.get("lines") if isinstance(cue, dict) else []
        if not isinstance(cue_lines, list):
            continue
        for line_index, line in enumerate(cue_lines):
            if not isinstance(line, dict):
                continue
            advance = float(line.get("advance_px") or 0.0)
            x = anchor_x - advance / 2.0
            baseline = baseline_y - (len(cue_lines) - line_index - 1) * line_height
            for run in line.get("runs") or []:
                if not isinstance(run, dict):
                    continue
                text = ass_escape(str(run.get("text") or ""))
                run_advance = float(run.get("advance_px") or 0.0)
                if not text:
                    x += run_advance
                    continue
                role = "Emphasis" if run.get("role") == "emphasis" else "Normal"
                top_y = baseline - float(run.get("baseline_offset_px") or 0.0)
                tags = ["\\an7", f"\\pos({round(x)},{round(top_y)})"]
                effect = str(run.get("effect_id") or "none")
                if effect == "soft_in":
                    tags.append("\\fad(120,0)")
                elif effect == "pop":
                    tags.extend(
                        [
                            "\\fscx85\\fscy85",
                            "\\t(0,120,\\fscx105\\fscy105)",
                            "\\t(120,240,\\fscx100\\fscy100)",
                        ]
                    )
                start = int(run.get("enter_frame") or 0) / fps
                end = int(run.get("exit_frame") or 0) / fps
                if end > start:
                    lines.append(
                        f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{role},,0,0,0,,"
                        + "{" + "".join(tags) + "}"
                        + text
                    )
                x += run_advance
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return []


def _style_row(
    name: str,
    font_name: str,
    font_size: int,
    *,
    primary: object,
    outline_color: object,
    outline: object,
) -> str:
    return (
        f"Style: {name},{font_name},{font_size},{_ass_color(primary, '#FFFFFF')},"
        f"&H000000FF,{_ass_color(outline_color, '#000000')},&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{_ass_outline(outline)},0,7,"
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


def _ass_outline(value: object, fallback: float = 4.0) -> str:
    try:
        return f"{max(0.0, float(value if value is not None else fallback)):g}"
    except (TypeError, ValueError):
        return f"{fallback:g}"


def ass_font_size(requested_size: object, *, height: int) -> int:
    try:
        base_size = int(requested_size or 64)
    except (TypeError, ValueError):
        base_size = 64
    scale = max(1.0, float(height or 1080) / 1080.0)
    return max(12, int(round(base_size * scale)))
