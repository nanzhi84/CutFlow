"""Controlled huazi presentation allowlists."""

from __future__ import annotations

HUAZI_PLACEMENTS: dict[str, dict[str, float | int]] = {
    "top_center_banner": {"x": 0.5, "y": 0.14, "align": 8},
    "upper_left_badge": {"x": 0.12, "y": 0.18, "align": 7},
    "upper_right_badge": {"x": 0.88, "y": 0.18, "align": 9},
    "mid_left_callout": {"x": 0.12, "y": 0.46, "align": 4},
    "mid_right_callout": {"x": 0.88, "y": 0.46, "align": 6},
    "lower_left_tag": {"x": 0.12, "y": 0.72, "align": 1},
    "lower_right_tag": {"x": 0.88, "y": 0.72, "align": 3},
}

HUAZI_ANIMATIONS = ("none", "fade_in", "pop_in", "slide_up", "slide_left", "slide_right", "punch")
HUAZI_SFX = ("none",)

# Slide animations carry an enter direction; used to validate that a chosen
# animation is compatible with the layout box's ``allowed_enter_directions``.
HUAZI_ANIMATION_DIRECTIONS = {"slide_up": "up", "slide_left": "left", "slide_right": "right"}


def huazi_placement(placement_id: str | None) -> dict[str, float | int]:
    resolved = str(placement_id or "").strip()
    return HUAZI_PLACEMENTS.get(resolved, HUAZI_PLACEMENTS["top_center_banner"])


def placement_candidates() -> list[dict[str, str]]:
    return [{"placement_id": placement_id} for placement_id in HUAZI_PLACEMENTS]


def animation_candidates() -> list[dict[str, str]]:
    return [{"animation_id": animation_id} for animation_id in HUAZI_ANIMATIONS]


def sfx_candidates() -> list[dict[str, str]]:
    return [{"sfx_id": sfx_id} for sfx_id in HUAZI_SFX]
