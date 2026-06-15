"""ASS subtitle authoring for the SubtitleAndBgmMix node."""

from __future__ import annotations

from pathlib import Path


def ass_time(seconds: float) -> str:
    centiseconds = round(max(seconds, 0) * 100)
    hours, remainder = divmod(centiseconds, 3600 * 100)
    minutes, remainder = divmod(remainder, 60 * 100)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("{", "").replace("}", "").replace("\n", r"\N")


def write_ass_subtitles(
    output_path: Path,
    *,
    narration: dict,
    style: dict,
    width: int,
    height: int,
    font_name: str | None = None,
) -> None:
    subtitle = style.get("subtitle", {}) if isinstance(style.get("subtitle"), dict) else {}
    font_size = int(subtitle.get("font_size") or 64)
    # libass matches the ASS ``Fontname`` against the family names of fonts in its
    # fontsdir; a resolved selection (from the uploaded .ttf/.otf) replaces the
    # hard-coded Arial so the user/agent-chosen font is actually burned. ASS field
    # values are comma-separated, so a family name containing commas would corrupt
    # the style row -- strip them and fall back to Arial when nothing usable.
    resolved_font = (font_name or "").replace(",", " ").strip() or "Arial"
    margin_v = int(height * 0.12)
    position = subtitle.get("position")
    if isinstance(position, dict) and "y" in position:
        margin_v = max(20, int(height * (1 - float(position["y"]))))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,{resolved_font},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
            f"1,0,0,0,100,100,0,0,1,4,1,2,80,80,{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for unit in narration.get("units", []):
        text = ass_escape(str(unit.get("text", "")))
        if not text:
            continue
        lines.append(
            "Dialogue: 0,"
            f"{ass_time(float(unit.get('start', 0) or 0))},"
            f"{ass_time(float(unit.get('end', 0) or 0))},"
            f"Default,,0,0,0,,{text}"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
