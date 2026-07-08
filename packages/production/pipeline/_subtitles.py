"""ASS subtitle authoring for the SubtitleAndBgmMix node."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from packages.production.pipeline._caption_styles import (
    HUAZI_ANIMATIONS,
    HUAZI_SFX,
    huazi_placement,
)

_ASS_MARGIN_L = 80
_ASS_MARGIN_R = 80
_ASS_WRAP_BREAK_CHARS = set("，,、：:；;。！？!? ")
_OVERLAY_STYLE_ALIASES = {"emphasis": "Emphasis"}


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


def _ass_color(value: str | None, fallback: str) -> str:
    red, green, blue = _parse_hex_rgb(value) or _parse_hex_rgb(fallback) or (255, 255, 255)
    return f"&H00{blue:02X}{green:02X}{red:02X}"


def _parse_hex_rgb(value: str | None) -> tuple[int, int, int] | None:
    text = str(value or "").strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _ass_outline(value, fallback: float = 4.0) -> str:
    try:
        outline = max(0.0, float(value))
    except (TypeError, ValueError):
        outline = fallback
    return f"{outline:g}"


def _ass_bold(value, fallback: bool = True) -> str:
    try:
        return "1" if int(value) >= 600 else "0"
    except (TypeError, ValueError):
        return "1" if fallback else "0"


def _overlay_style_name(value: object) -> str:
    return _OVERLAY_STYLE_ALIASES.get(str(value or "").strip().lower(), "Emphasis")


def _overlay_style_rows(
    *,
    font_name: str,
    font_size: int,
    margin_v: int,
    subtitle: dict,
) -> list[str]:
    emphasis_primary = _ass_color(subtitle.get("emphasis_primary_color"), "#FFFF00")
    emphasis_outline = _ass_color(subtitle.get("emphasis_outline_color"), "#000000")
    outline = subtitle.get("emphasis_outline")
    if outline is None:
        outline = subtitle.get("outline")
    return [
        (
            f"Style: Emphasis,{font_name},{font_size},{emphasis_primary},&H000000FF,"
            f"{emphasis_outline},&H64000000,"
            f"{_ass_bold(subtitle.get('emphasis_font_weight'))},0,0,0,100,100,0,0,1,"
            f"{_ass_outline(outline, 5.0)},1,8,"
            f"{_ASS_MARGIN_L},{_ASS_MARGIN_R},{margin_v},1"
        ),
    ]


def _normal_bold(subtitle: dict) -> str:
    return _ass_bold(subtitle.get("font_weight"), fallback=True)


def _overlay_animation_id(value: object) -> str:
    text = str(value or "").strip()
    return text if text in HUAZI_ANIMATIONS else "pop_in"


def _overlay_sfx_id(value: object) -> str:
    text = str(value or "").strip()
    return text if text in HUAZI_SFX else "none"


def _overlay_override_tags(
    event: dict[str, Any],
    *,
    width: int,
    height: int,
    subtitle: dict,
) -> str:
    placement_id = str(
        event.get("placement_id")
        or subtitle.get("default_emphasis_position_id")
        or "top_center_banner"
    )
    placement = huazi_placement(placement_id)
    align = int(placement.get("align") or 8)
    x = int(round(width * float(placement.get("x") or 0.5)))
    y = int(round(height * float(placement.get("y") or 0.14)))
    animation = _overlay_animation_id(
        event.get("animation_id") or subtitle.get("default_emphasis_animation_id")
    )
    # First version records sfx intent in the event/manifest but does not synthesize audio.
    # Keep non-none ids out of ASS rather than pretending a sound was mixed.
    _overlay_sfx_id(event.get("sfx_id"))
    tags = [f"\\an{align}"]
    if animation == "slide_up":
        tags.append(f"\\move({x},{y + 80},{x},{y},0,220)")
        tags.append(r"\fad(80,120)")
    elif animation == "slide_left":
        tags.append(f"\\move({x + 90},{y},{x},{y},0,220)")
        tags.append(r"\fad(80,120)")
    else:
        tags.append(f"\\pos({x},{y})")
        if animation == "fade_in":
            tags.append(r"\fad(180,100)")
        elif animation == "pop_in":
            tags.append(r"\fad(80,120)\t(0,180,\fscx108\fscy108)")
        elif animation == "punch":
            tags.append(r"\t(0,120,\fscx116\fscy116)\t(120,260,\fscx100\fscy100)")
    return "{" + "".join(tags) + "}"


def write_ass_subtitles(
    output_path: Path,
    *,
    narration: dict,
    style: dict,
    width: int,
    height: int,
    font_name: str | None = None,
    overlay_events: list[dict] | None = None,
) -> None:
    subtitle = style.get("subtitle", {}) if isinstance(style.get("subtitle"), dict) else {}
    font_size = ass_font_size(subtitle.get("font_size"), height=height)
    overlay_events = overlay_events or []
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
    # Emphasis (花字) banner sizing: larger, top-anchored so it layers above the normal
    # bottom subtitle without overlapping it.
    try:
        emphasis_scale = float(subtitle.get("emphasis_size_scale") or 1.4)
    except (TypeError, ValueError):
        emphasis_scale = 1.4
    emphasis_size = int(round(font_size * max(1.0, emphasis_scale)))
    emphasis_margin_v = int(height * 0.12)
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
            f"Style: Default,{resolved_font},{font_size},"
            f"{_ass_color(subtitle.get('primary_color'), '#FFFFFF')},&H000000FF,"
            f"{_ass_color(subtitle.get('outline_color'), '#000000')},&H64000000,"
            f"{_normal_bold(subtitle)},0,0,0,100,100,0,0,1,"
            f"{_ass_outline(subtitle.get('outline'), 4.0)},"
            f"1,2,{_ASS_MARGIN_L},{_ASS_MARGIN_R},{margin_v},1"
        ),
    ]
    # The Emphasis style row is emitted ONLY when there are overlay events. Without
    # overlays, the subtitle style table stays unchanged. Yellow, larger, top-centered.
    if overlay_events:
        lines.extend(
            _overlay_style_rows(
                font_name=resolved_font,
                font_size=emphasis_size,
                margin_v=emphasis_margin_v,
                subtitle=subtitle,
            )
        )
    lines += [
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
    # Emphasis overlays on Layer 1 (above the Layer 0 narration). Each carries the key
    # phrase itself, timed to the narration sentence StylePlanning matched it to.
    for event in overlay_events:
        text = ass_escape(
            ass_wrap_text(
                str(event.get("text", "")),
                width=width,
                font_size=emphasis_size,
                margin_l=_ASS_MARGIN_L,
                margin_r=_ASS_MARGIN_R,
            )
        )
        if not text:
            continue
        lines.append(
            "Dialogue: 1,"
            f"{ass_time(float(event.get('start', 0) or 0))},"
            f"{ass_time(float(event.get('end', 0) or 0))},"
            f"{_overlay_style_name(event.get('style'))},,0,0,0,,"
            f"{_overlay_override_tags(event, width=width, height=height, subtitle=subtitle)}"
            f"{text}"
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
