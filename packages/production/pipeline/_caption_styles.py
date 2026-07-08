"""Controlled caption style-pair presets and huazi presentation allowlists."""

from __future__ import annotations

from typing import Any

DEFAULT_CAPTION_STYLE_PAIR_ID = "douyin_bold_a"

CAPTION_STYLE_PAIRS: dict[str, dict[str, Any]] = {
    "douyin_bold_a": {
        "label": "Douyin bold A",
        "normal": {
            "font_weight": 600,
            "font_size": 64,
            "size_scale": 1.0,
            "color": "#FFFFFF",
            "outline_color": "#000000",
            "outline": 4.0,
            "position": {"x": 0.5, "y": 0.88},
        },
        "huazi": {
            "font_weight": 900,
            "size_scale": 1.45,
            "color": "#FFE84A",
            "outline_color": "#000000",
            "outline": 5.0,
            "default_placement_id": "top_center_banner",
            "default_animation_id": "pop_in",
        },
    },
    "clean_editorial_b": {
        "label": "Clean editorial B",
        "normal": {
            "font_weight": 500,
            "font_size": 52,
            "size_scale": 1.0,
            "color": "#F8FAFC",
            "outline_color": "#111827",
            "outline": 3.0,
            "position": {"x": 0.5, "y": 0.86},
        },
        "huazi": {
            "font_weight": 700,
            "size_scale": 1.28,
            "color": "#BAE6FD",
            "outline_color": "#0F172A",
            "outline": 4.0,
            "default_placement_id": "upper_left_badge",
            "default_animation_id": "fade_in",
        },
    },
    "local_promo_c": {
        "label": "Local promo C",
        "normal": {
            "font_weight": 600,
            "font_size": 60,
            "size_scale": 1.0,
            "color": "#FFFFFF",
            "outline_color": "#111111",
            "outline": 4.5,
            "position": {"x": 0.5, "y": 0.89},
        },
        "huazi": {
            "font_weight": 900,
            "size_scale": 1.38,
            "color": "#FF6B35",
            "outline_color": "#FFFFFF",
            "outline": 3.5,
            "default_placement_id": "upper_right_badge",
            "default_animation_id": "punch",
        },
    },
}

LEGACY_STYLE_PAIR_MAP = {
    "douyin": "douyin_bold_a",
    "variety": "douyin_bold_a",
    "youshe_title_black": "douyin_bold_a",
    "clean": "clean_editorial_b",
    "movie": "clean_editorial_b",
    "news": "local_promo_c",
}

HUAZI_PLACEMENTS: dict[str, dict[str, float | int]] = {
    "top_center_banner": {"x": 0.5, "y": 0.14, "align": 8},
    "upper_left_badge": {"x": 0.12, "y": 0.18, "align": 7},
    "upper_right_badge": {"x": 0.88, "y": 0.18, "align": 9},
    "mid_left_callout": {"x": 0.12, "y": 0.46, "align": 4},
    "mid_right_callout": {"x": 0.88, "y": 0.46, "align": 6},
    "lower_left_tag": {"x": 0.12, "y": 0.72, "align": 1},
    "lower_right_tag": {"x": 0.88, "y": 0.72, "align": 3},
}

HUAZI_ANIMATIONS = ("none", "fade_in", "pop_in", "slide_up", "slide_left", "punch")
HUAZI_SFX = ("none",)


def resolve_caption_style_pair_id(
    caption_style_pair_id: str | None,
    style_preset: str | None,
) -> str:
    explicit = str(caption_style_pair_id or "").strip()
    if explicit in CAPTION_STYLE_PAIRS:
        return explicit
    legacy = str(style_preset or "").strip()
    return LEGACY_STYLE_PAIR_MAP.get(legacy, DEFAULT_CAPTION_STYLE_PAIR_ID)


def caption_style_pair(pair_id: str | None) -> dict[str, Any]:
    resolved = pair_id if pair_id in CAPTION_STYLE_PAIRS else DEFAULT_CAPTION_STYLE_PAIR_ID
    return CAPTION_STYLE_PAIRS[resolved]


def huazi_placement(placement_id: str | None) -> dict[str, float | int]:
    resolved = str(placement_id or "").strip()
    return HUAZI_PLACEMENTS.get(resolved, HUAZI_PLACEMENTS["top_center_banner"])


def placement_candidates() -> list[dict[str, str]]:
    return [{"placement_id": placement_id} for placement_id in HUAZI_PLACEMENTS]


def animation_candidates() -> list[dict[str, str]]:
    return [{"animation_id": animation_id} for animation_id in HUAZI_ANIMATIONS]


def sfx_candidates() -> list[dict[str, str]]:
    return [{"sfx_id": sfx_id} for sfx_id in HUAZI_SFX]
