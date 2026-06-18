"""ASS subtitle authoring for the SubtitleAndBgmMix node."""

from __future__ import annotations

import unicodedata
from pathlib import Path

_ASS_MARGIN_L = 80
_ASS_MARGIN_R = 80
_ASS_WRAP_BREAK_CHARS = set("，,、：:；;。！？!? ")


def ass_time(seconds: float) -> str:
    centiseconds = round(max(seconds, 0) * 100)
    hours, remainder = divmod(centiseconds, 3600 * 100)
    minutes, remainder = divmod(remainder, 60 * 100)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("{", "").replace("}", "").replace("\n", r"\N")


def _subtitle_char_units(char: str) -> float:
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 1.0
    if char.isspace():
        return 0.35
    return 0.58


def _subtitle_visual_units(text: str) -> float:
    return sum(_subtitle_char_units(char) for char in text)


def _last_break_index(text: str) -> int:
    for index in range(len(text) - 1, -1, -1):
        if text[index] in _ASS_WRAP_BREAK_CHARS:
            return index
    return -1


def _wrap_subtitle_paragraph(paragraph: str, max_units: float) -> list[str]:
    paragraph = paragraph.strip()
    if not paragraph:
        return []

    lines: list[str] = []
    buffer = ""
    for char in paragraph:
        buffer += char
        if _subtitle_visual_units(buffer) <= max_units:
            continue

        break_index = _last_break_index(buffer)
        if 0 <= break_index < len(buffer) - 1:
            split_at = break_index + 1
        else:
            split_at = max(1, len(buffer) - 1)

        line = buffer[:split_at].strip()
        if line:
            lines.append(line)
        buffer = buffer[split_at:].lstrip()

    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def ass_wrap_text(
    text: str,
    *,
    width: int,
    font_size: int,
    margin_l: int = _ASS_MARGIN_L,
    margin_r: int = _ASS_MARGIN_R,
) -> str:
    available_width = max(font_size * 4, int(width or 0) - margin_l - margin_r)
    max_units = max(4.0, available_width / max(float(font_size) * 0.95, 1.0))
    wrapped: list[str] = []
    for paragraph in str(text).splitlines():
        wrapped.extend(_wrap_subtitle_paragraph(paragraph, max_units))
    return "\n".join(wrapped)


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
    font_size = ass_font_size(subtitle.get("font_size"), height=height)
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
            f"1,0,0,0,100,100,0,0,1,4,1,2,{_ASS_MARGIN_L},{_ASS_MARGIN_R},{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for unit in narration.get("units", []):
        text = ass_escape(
            ass_wrap_text(
                str(unit.get("text", "")),
                width=width,
                font_size=font_size,
                margin_l=_ASS_MARGIN_L,
                margin_r=_ASS_MARGIN_R,
            )
        )
        if not text:
            continue
        lines.append(
            "Dialogue: 0,"
            f"{ass_time(float(unit.get('start', 0) or 0))},"
            f"{ass_time(float(unit.get('end', 0) or 0))},"
            f"Default,,0,0,0,,{text}"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ass_font_size(requested_size, *, height: int) -> int:
    """Convert the UI's 1080p-baseline subtitle size into ASS PlayRes units."""
    try:
        base_size = int(requested_size or 64)
    except (TypeError, ValueError):
        base_size = 64
    scale = max(1.0, float(height or 1080) / 1080.0)
    return max(12, int(round(base_size * scale)))
