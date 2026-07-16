"""ASS authoring for the fixed-band caption-composition artifact."""

from __future__ import annotations

from pathlib import Path

from packages.core.contracts.artifacts import CaptionCompositionPlanArtifact, StylePlanArtifact
from packages.production.pipeline._fonts import is_ass_bold_weight

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
) -> list[str]:
    """Write one Dialogue per CaptionRun; line breaks and x positions are preplanned."""

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
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    fps = caption_composition.fps
    for cue in caption_composition.cues:
        cue_lines = cue.lines
        for line_index, line in enumerate(cue_lines):
            advance = line.advance_px
            x = anchor_x - advance / 2.0
            baseline = baseline_y - (len(cue_lines) - line_index - 1) * line_height
            for run in line.runs:
                text = ass_escape(run.text)
                run_advance = run.advance_px
                role = "Emphasis" if run.role == "emphasis" else "Normal"
                top_y = baseline - run.baseline_offset_px
                tags = ["\\an7", f"\\pos({round(x)},{round(top_y)})"]
                effect = run.effect_id
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
                start = run.enter_frame / fps
                end = run.exit_frame / fps
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
