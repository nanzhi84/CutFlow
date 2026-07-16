"""Deterministic emphasis-style templates and semantic selection."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from packages.core.contracts.artifacts import EmphasisHint

BackingKind = Literal["burst_star", "underline_swipe", "highlight_rect"]


@dataclass(frozen=True)
class EmphasisStyleSpec:
    style_id: str
    font_asset_id: str
    fill: str
    outline: str
    outline_width: float
    size_ratio: float
    backing: BackingKind | None
    backing_color: str | None
    effect_id: str
    sfx_class: str | None
    sfx_volume: float


_STYLES = (
    EmphasisStyleSpec(
        "classic_yellow",
        "asset_font_noto_sans_cjk_sc_bold",
        "#FFE14D",
        "#000000",
        4.0,
        1.40,
        None,
        None,
        "pop",
        "pop",
        0.48,
    ),
    EmphasisStyleSpec(
        "blue_burst",
        "asset_font_zcool_qingke_huangyou",
        "#FFFFFF",
        "#1E6FD9",
        5.0,
        1.50,
        "burst_star",
        "#3FA7F5",
        "pop_rotate",
        "impact",
        0.48,
    ),
    EmphasisStyleSpec(
        "red_alert",
        "asset_font_noto_sans_cjk_sc_bold",
        "#FF4D4F",
        "#FFFFFF",
        4.0,
        1.45,
        None,
        None,
        "drop_in",
        "impact",
        0.48,
    ),
    EmphasisStyleSpec(
        "brand_stamp",
        "asset_font_smiley_sans",
        "#A8D8F0",
        "#1B4F8A",
        5.0,
        1.50,
        None,
        None,
        "zoom_settle",
        "ding",
        0.48,
    ),
    EmphasisStyleSpec(
        "marker_orange",
        "asset_font_lxgw_marker",
        "#FF8A00",
        "#FFFFFF",
        3.5,
        1.40,
        "underline_swipe",
        "#FFD34D",
        "slide_up_in",
        "whoosh",
        0.48,
    ),
    EmphasisStyleSpec(
        "ink_hand",
        "asset_font_mashanzheng",
        "#FFFFFF",
        "#000000",
        3.0,
        1.55,
        None,
        None,
        "soft_in",
        None,
        0.0,
    ),
    EmphasisStyleSpec(
        "gold_serif",
        "asset_font_noto_serif_cjk_sc_bold",
        "#E8C97A",
        "#4A3418",
        3.0,
        1.35,
        None,
        None,
        "soft_in",
        None,
        0.0,
    ),
    EmphasisStyleSpec(
        "highlight_box",
        "asset_font_noto_sans_cjk_sc_bold",
        "#111111",
        "#000000",
        0.0,
        1.30,
        "highlight_rect",
        "#FFE14D",
        "soft_in",
        "click",
        0.48,
    ),
)

EMPHASIS_STYLES = {style.style_id: style for style in _STYLES}

_STYLE_GROUPS = {
    "playful": ("blue_burst", "marker_orange", "classic_yellow"),
    "premium": ("gold_serif", "ink_hand", "brand_stamp"),
    "urgent": ("red_alert", "classic_yellow", "highlight_box"),
    "warm": ("classic_yellow", "marker_orange", "gold_serif"),
    "default": ("classic_yellow", "blue_burst", "gold_serif"),
}


def emphasis_style(style_id: str) -> EmphasisStyleSpec:
    try:
        return EMPHASIS_STYLES[style_id]
    except KeyError as exc:
        raise ValueError(f"unknown emphasis style: {style_id}") from exc


def select_emphasis_styles(
    hints: list[EmphasisHint],
    *,
    tone: object = None,
    bgm_mood: object = None,
    requested_style_id: str | None = None,
) -> list[EmphasisStyleSpec]:
    """Map low-cardinality intent to templates without random state."""

    if requested_style_id is not None:
        forced = emphasis_style(requested_style_id)
        return [forced] * len(hints)
    group = _STYLE_GROUPS[_style_group(tone, bgm_mood)]
    selected: list[EmphasisStyleSpec] = []
    for index, hint in enumerate(hints):
        if hint.display_mode == "whole_cue":
            selected.append(emphasis_style("brand_stamp"))
            continue
        candidates = group
        if hint.intensity == "hero":
            backed = tuple(
                style_id for style_id in group if emphasis_style(style_id).backing is not None
            )
            candidates = backed or ("highlight_box", "blue_burst", "marker_orange")
        selected.append(emphasis_style(candidates[index % len(candidates)]))
    return selected


def emphasis_style_horizontal_padding(styles: list[EmphasisStyleSpec]) -> float:
    """Return conservative left+right outline space for fixed-band layout."""

    return 2.0 * max((style.outline_width for style in styles), default=0.0)


def emphasis_run_vertical_margins(
    *,
    style_id: str,
    effect_id: str,
    font_size: int,
    advance_px: float,
) -> tuple[float, float]:
    """Conservative pixels above/below the run's unanimated text box."""

    style = emphasis_style(style_id)
    above = style.outline_width
    below = style.outline_width
    if style.backing in {"burst_star", "highlight_rect"}:
        above = max(above, 5.0)
        below = max(below, 5.0)
    if effect_id == "slide_up_in":
        below += 22.0
    elif effect_id == "pop":
        below += font_size * 0.05
    elif effect_id == "pop_rotate":
        scale_growth = font_size * 0.08
        rotation_growth = advance_px * math.sin(math.radians(6.0))
        above += rotation_growth
        below += scale_growth + rotation_growth
    elif effect_id == "jelly_pop":
        below += font_size * 0.05
    elif effect_id == "drop_in":
        above += 26.0
    elif effect_id == "zoom_settle":
        below += font_size * 0.30
    return above, below


def _style_group(tone: object, bgm_mood: object) -> str:
    values = {str(tone or "").strip(), str(bgm_mood or "").strip()}
    if values.intersection({"俏皮", "轻快"}):
        return "playful"
    if values.intersection({"高级", "沉稳"}):
        return "premium"
    if values.intersection({"高能", "紧张"}):
        return "urgent"
    if values.intersection({"温暖", "励志"}):
        return "warm"
    return "default"
