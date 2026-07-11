"""ASS subtitle authoring for the SubtitleAndBgmMix node."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from packages.production.pipeline._caption_styles import (
    HUAZI_ANIMATIONS,
    huazi_placement,
)
from packages.production.pipeline._caption_effects import (
    CAPTION_V3_EFFECTS,
    normal_soft_in_tags,
    overlay_effect_tags,
)
from packages.production.pipeline._keyword_highlight import highlighted_spans

_ASS_MARGIN_L = 80
_ASS_MARGIN_R = 80
_ASS_WRAP_BREAK_CHARS = set("，,、：:；;。！？!? ")
_OVERLAY_STYLE_ALIASES = {"emphasis": "Emphasis", "hero": "Hero"}

# Emphasis animation timing. Events shorter than the threshold shrink the
# animation span to 40% of the event so it plays fully inside the event window;
# ``_ANIM_NATURAL_MS`` is each animation's natural span used as the shrink base.
_ANIM_CLAMP_THRESHOLD_SEC = 0.55
_ANIM_SHRINK_RATIO = 0.4
_ANIM_NATURAL_MS = {
    "fade_in": 180,
    "pop_in": 180,
    "punch": 260,
    "slide_up": 220,
    "slide_left": 220,
    "slide_right": 220,
    "soft_in": 140,
    "pop": 300,
    "slam_scale": 240,
}
_DEFAULT_EMPHASIS_ANIMATION = "pop_in"


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


def _overlay_style_name(event: dict[str, Any]) -> str:
    value = event.get("visual_preset_id") or event.get("style")
    return _OVERLAY_STYLE_ALIASES.get(str(value or "").strip().lower(), "Emphasis")


def _overlay_style_rows(
    *,
    emphasis_font_name: str,
    font_size: int,
    margin_v: int,
    subtitle: dict,
    preset_ids: set[str],
    fixed_ratios: bool,
) -> list[str]:
    emphasis_primary = _ass_color(
        subtitle.get("primary_color") if fixed_ratios else subtitle.get("emphasis_primary_color"),
        "#FFFFFF" if fixed_ratios else "#FFFF00",
    )
    emphasis_outline = _ass_color(subtitle.get("emphasis_outline_color"), "#000000")
    outline = subtitle.get("emphasis_outline")
    if outline is None:
        outline = subtitle.get("outline")
    rows = []
    if "emphasis" in preset_ids:
        rows.append(
            f"Style: Emphasis,{emphasis_font_name},"
            f"{round(font_size * 1.25) if fixed_ratios else font_size},"
            f"{emphasis_primary},&H000000FF,{emphasis_outline},&H64000000,"
            f"{_ass_bold(subtitle.get('emphasis_font_weight'))},0,0,0,100,100,0,0,1,"
            f"{_ass_outline(outline, 5.0)},1,8,"
            f"{_ASS_MARGIN_L},{_ASS_MARGIN_R},{margin_v},1"
        )
    if "hero" in preset_ids:
        rows.append(
            f"Style: Hero,{emphasis_font_name},"
            f"{round(font_size * 2.2) if fixed_ratios else font_size},"
            f"{emphasis_primary},&H000000FF,{emphasis_outline},&H64000000,"
            f"{_ass_bold(subtitle.get('emphasis_font_weight'))},0,0,0,100,100,0,0,1,"
            f"{_ass_outline(outline, 5.0)},1,5,"
            f"{_ASS_MARGIN_L},{_ASS_MARGIN_R},{margin_v},1"
        )
    return rows


def _normal_bold(subtitle: dict) -> str:
    return _ass_bold(subtitle.get("font_weight"), fallback=True)


def _resolve_animation(value: object, default: object = None) -> tuple[str, bool]:
    """Resolve an overlay animation id against the whitelist.

    A missing/empty id falls back to the default emphasis animation (not a
    fallback event). A non-empty *unknown* id renders as static ``none`` and is
    reported as ``huazi.animation_fallback`` -- never silently coerced to pop_in.
    Returns ``(animation_id, is_fallback)``.
    """
    raw = str(value or default or "").strip()
    if not raw:
        return _DEFAULT_EMPHASIS_ANIMATION, False
    if raw in HUAZI_ANIMATIONS or raw in CAPTION_V3_EFFECTS:
        return raw, False
    return "none", True


def _scale_ms(value: int, scale: float) -> int:
    return max(1, int(round(value * scale)))


def _animation_scale(animation: str, start: float, end: float) -> float:
    """Shrink an animation's timings when the event is very short (else 1.0).

    Events >= 0.55s keep their natural timing; shorter events cap the animation
    span at 40% of the event duration so it fully plays within the event window.
    """
    duration = end - start
    if duration <= 0 or duration >= _ANIM_CLAMP_THRESHOLD_SEC:
        return 1.0
    natural = _ANIM_NATURAL_MS.get(animation, 0)
    if natural <= 0:
        return 1.0
    budget_ms = duration * 1000.0 * _ANIM_SHRINK_RATIO
    return min(1.0, budget_ms / natural)


def _animation_body(animation: str, x: int, y: int, scale: float) -> list[str]:
    """ASS override tags for one animation anchored at pixel (x, y).

    Only time parameters are scaled; coordinates are never touched. slide_*
    animations enter from an off-box offset toward (x, y).
    """
    if animation in CAPTION_V3_EFFECTS:
        return overlay_effect_tags(animation, x=x, y=y)
    if animation in ("slide_up", "slide_left", "slide_right"):
        if animation == "slide_up":
            start_x, start_y = x, y + 80
        elif animation == "slide_left":
            start_x, start_y = x + 90, y
        else:  # slide_right
            start_x, start_y = x - 90, y
        return [
            f"\\move({start_x},{start_y},{x},{y},0,{_scale_ms(220, scale)})",
            f"\\fad({_scale_ms(80, scale)},{_scale_ms(120, scale)})",
        ]
    body = [f"\\pos({x},{y})"]
    if animation == "fade_in":
        body.append(f"\\fad({_scale_ms(180, scale)},{_scale_ms(100, scale)})")
    elif animation == "pop_in":
        body.append(
            f"\\fad({_scale_ms(80, scale)},{_scale_ms(120, scale)})"
            f"\\t(0,{_scale_ms(180, scale)},\\fscx108\\fscy108)"
        )
    elif animation == "punch":
        body.append(
            f"\\t(0,{_scale_ms(120, scale)},\\fscx116\\fscy116)"
            f"\\t({_scale_ms(120, scale)},{_scale_ms(260, scale)},\\fscx100\\fscy100)"
        )
    # "none" (and any other static-position animation) emits just \pos.
    return body


def _rect_anchor(text_align: str, rect: dict, width: int, height: int) -> tuple[int, int, int]:
    """(\\an, x_px, y_px) for a rect box: vertically centered, horizontally aligned."""
    x = float(rect.get("x") or 0.0)
    y = float(rect.get("y") or 0.0)
    w = float(rect.get("w") or 0.0)
    h = float(rect.get("h") or 0.0)
    center_y = int(round((y + h / 2.0) * height))
    align = str(text_align or "center").strip().lower()
    if align == "left":
        return 4, int(round(x * width)), center_y
    if align == "right":
        return 6, int(round((x + w) * width)), center_y
    return 5, int(round((x + w / 2.0) * width)), center_y


def _overlay_rect_tags(event: dict[str, Any], *, width: int, height: int) -> tuple[str, bool]:
    """Override tags for a materialized-rect overlay (D7 render path).

    Geometry comes from ``rect`` + ``text_align``; the animation timing shrinks for
    very short events. Returns ``(tag_string, animation_fallback)``.
    """
    rect = event.get("rect") or {}
    align, x, y = _rect_anchor(str(event.get("text_align") or "center"), rect, width, height)
    animation, is_fallback = _resolve_animation(event.get("animation_id"))
    scale = _animation_scale(
        animation, float(event.get("start") or 0.0), float(event.get("end") or 0.0)
    )
    tags = [f"\\an{align}"] + _animation_body(animation, x, y, scale)
    return "{" + "".join(tags) + "}", is_fallback


def _overlay_placement_tags(
    event: dict[str, Any],
    *,
    width: int,
    height: int,
    subtitle: dict,
) -> tuple[str, bool]:
    """Legacy placement_id override path (rect-less / pre-#188 overlay events).

    Byte-identical geometry/timing to the pre-#188 renderer for known animations;
    an unknown animation now renders ``none`` + fallback instead of silent pop_in.
    """
    placement_id = str(
        event.get("placement_id")
        or subtitle.get("default_emphasis_position_id")
        or "top_center_banner"
    )
    placement = huazi_placement(placement_id)
    align = int(placement.get("align") or 8)
    x = int(round(width * float(placement.get("x") or 0.5)))
    y = int(round(height * float(placement.get("y") or 0.14)))
    animation, is_fallback = _resolve_animation(
        event.get("animation_id"), subtitle.get("default_emphasis_animation_id")
    )
    tags = [f"\\an{align}"] + _animation_body(animation, x, y, 1.0)
    return "{" + "".join(tags) + "}", is_fallback


def write_ass_subtitles(
    output_path: Path,
    *,
    style: dict,
    width: int,
    height: int,
    narration: dict | None = None,
    caption_cues: list[dict] | None = None,
    font_name: str | None = None,
    emphasis_font_name: str | None = None,
    overlay_events: list[dict] | None = None,
) -> list[str]:
    """Author the ASS subtitle file; return the event ids whose animation fell back.

    Normal captions come from ``caption_cues`` (already line-broken by the display
    compiler, ``lines`` joined with ``\\N``) when provided; otherwise the legacy
    ``narration`` greedy-wrap path runs (kept for the deterministic-chain tests).
    ``WrapStyle: 2`` disables libass auto-wrapping so manual breaks are honoured
    (D11). Overlay events with a materialized ``rect`` use the rect render path;
    rect-less events fall back to the legacy placement_id geometry.
    """
    subtitle = style.get("subtitle", {}) if isinstance(style.get("subtitle"), dict) else {}
    normal_enabled = _subtitle_layer_enabled(subtitle, "normal_enabled")
    emphasis_enabled = _subtitle_layer_enabled(subtitle, "emphasis_enabled")
    font_size = ass_font_size(subtitle.get("font_size"), height=height)
    overlay_events = overlay_events or []
    # libass matches the ASS ``Fontname`` against the family names of fonts in its
    # fontsdir; a resolved selection (from the uploaded .ttf/.otf) replaces the
    # hard-coded Arial so the user/agent-chosen font is actually burned. ASS field
    # values are comma-separated, so a family name containing commas would corrupt
    # the style row -- strip them and fall back to Arial when nothing usable.
    resolved_font = (font_name or "").replace(",", " ").strip() or "Arial"
    resolved_emphasis_font = (emphasis_font_name or font_name or "").replace(
        ",", " "
    ).strip() or resolved_font
    margin_v = int(height * 0.12)
    position = subtitle.get("position")
    if isinstance(position, dict) and "y" in position:
        margin_v = max(20, int(height * (1 - float(position["y"]))))
    # Emphasis (花字) uses its own user-selected size. Older style payloads that do
    # not carry it keep the previous larger-than-normal fallback.
    if subtitle.get("emphasis_font_size") is not None:
        emphasis_size = ass_font_size(subtitle.get("emphasis_font_size"), height=height)
    else:
        emphasis_size = int(round(font_size * 1.4))
    emphasis_margin_v = int(height * 0.12)
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
    if emphasis_enabled and overlay_events:
        fixed_ratios = any(event.get("visual_preset_id") for event in overlay_events)
        preset_ids = {str(event.get("visual_preset_id") or "emphasis") for event in overlay_events}
        lines.extend(
            _overlay_style_rows(
                emphasis_font_name=resolved_emphasis_font,
                font_size=font_size if fixed_ratios else emphasis_size,
                margin_v=emphasis_margin_v,
                subtitle=subtitle,
                preset_ids=preset_ids,
                fixed_ratios=fixed_ratios,
            )
        )
    lines += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if normal_enabled:
        for start, end, text in _normal_dialogues(
            caption_cues,
            narration,
            width,
            height,
            font_size,
            margin_v,
        ):
            if not text:
                continue
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    animation_fallbacks: list[str] = []
    # Emphasis overlays on Layer 1 (above the Layer 0 narration). Each carries the
    # key phrase itself, timed to the narration sentence it was matched to.
    if emphasis_enabled:
        for event in overlay_events:
            rect = event.get("rect")
            if isinstance(rect, dict) and rect:
                tags, is_fallback = _overlay_rect_tags(event, width=width, height=height)
                text = _overlay_text(event, subtitle)
            else:
                tags, is_fallback = _overlay_placement_tags(
                    event, width=width, height=height, subtitle=subtitle
                )
                wrapped = ass_wrap_text(
                    str(event.get("text", "")),
                    width=width,
                    font_size=emphasis_size,
                    margin_l=_ASS_MARGIN_L,
                    margin_r=_ASS_MARGIN_R,
                )
                text = _overlay_text({**event, "text": wrapped}, subtitle)
            if is_fallback:
                animation_fallbacks.append(str(event.get("event_id") or ""))
            if not text:
                continue
            lines.append(
                "Dialogue: 1,"
                f"{ass_time(float(event.get('start', 0) or 0))},"
                f"{ass_time(float(event.get('end', 0) or 0))},"
                f"{_overlay_style_name(event)},,0,0,0,,"
                f"{tags}"
                f"{text}"
            )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return animation_fallbacks


def _normal_dialogues(
    caption_cues: list[dict] | None,
    narration: dict | None,
    width: int,
    height: int,
    font_size: int,
    margin_v: int,
) -> list[tuple[float, float, str]]:
    """Yield ``(start, end, ass_text)`` for the normal caption layer.

    Prefers the compiler's pre-broken ``caption_cues``; falls back to the legacy
    per-unit greedy wrap when only ``narration`` is supplied.
    """
    rows: list[tuple[float, float, str]] = []
    if caption_cues is not None:
        for cue in caption_cues:
            lines = [ass_escape(line) for line in (cue.get("lines") or [])]
            start = float(cue.get("start") or 0.0)
            end = float(cue.get("end") or 0.0)
            effect = str(cue.get("effect_id") or "none")
            prefix = (
                normal_soft_in_tags(x=width // 2, y=max(0, height - margin_v))
                if effect == "soft_in"
                else ""
            )
            line_starts = [float(value) for value in (cue.get("line_starts") or [])]
            if len(lines) == 2 and len(line_starts) == 2 and start < line_starts[1] < end:
                rows.append((start, line_starts[1], prefix + lines[0]))
                rows.append((line_starts[1], end, "\\N".join(lines)))
            else:
                rows.append((start, end, prefix + "\\N".join(lines)))
        return rows
    for unit in (narration or {}).get("units", []):
        text = ass_escape(
            ass_wrap_text(
                str(unit.get("text", "")),
                width=width,
                font_size=font_size,
                margin_l=_ASS_MARGIN_L,
                margin_r=_ASS_MARGIN_R,
            )
        )
        rows.append((float(unit.get("start", 0) or 0), float(unit.get("end", 0) or 0), text))
    return rows


def _overlay_text(event: dict[str, Any], subtitle: dict) -> str:
    """Escape text first, then add only inline color override tags."""

    if not event.get("visual_preset_id"):
        return ass_escape(str(event.get("text") or ""))
    primary = _ass_color(subtitle.get("primary_color"), "#FFFFFF")
    highlight = _ass_color(subtitle.get("emphasis_primary_color"), "#FFFF00")
    parts = []
    for value, is_highlighted in highlighted_spans(str(event.get("text") or "")):
        escaped = ass_escape(value)
        if not escaped:
            continue
        if is_highlighted:
            parts.append(f"{{\\1c{highlight}}}{escaped}{{\\1c{primary}}}")
        else:
            parts.append(escaped)
    return "".join(parts)


def _subtitle_layer_enabled(subtitle: dict, key: str) -> bool:
    value = subtitle.get(key)
    return True if value is None else bool(value)


def ass_font_size(requested_size, *, height: int) -> int:
    """Convert the UI's 1080p-baseline subtitle size into ASS PlayRes units."""
    try:
        base_size = int(requested_size or 64)
    except (TypeError, ValueError):
        base_size = 64
    scale = max(1.0, float(height or 1080) / 1080.0)
    return max(12, int(round(base_size * scale)))
