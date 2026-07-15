"""The deliberately small Caption Liveliness v3 visual vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CaptionVisualPreset:
    preset_id: Literal["normal", "emphasis", "hero"]
    size_ratio: float
    color_mode: Literal["white", "dual"]
    effect_id: Literal["soft_in", "pop", "slam_scale"]
    max_animation_scale: float
    max_vertical_shift_px: float = 0.0


CAPTION_VISUAL_PRESETS: dict[str, CaptionVisualPreset] = {
    "normal": CaptionVisualPreset(
        preset_id="normal",
        size_ratio=1.0,
        color_mode="white",
        effect_id="soft_in",
        max_animation_scale=1.0,
        max_vertical_shift_px=14.0,
    ),
    "emphasis": CaptionVisualPreset(
        preset_id="emphasis",
        size_ratio=1.25,
        color_mode="dual",
        effect_id="pop",
        max_animation_scale=1.05,
    ),
    "hero": CaptionVisualPreset(
        preset_id="hero",
        size_ratio=2.2,
        color_mode="dual",
        effect_id="slam_scale",
        max_animation_scale=2.2,
    ),
}


def caption_visual_preset(preset_id: str) -> CaptionVisualPreset:
    return CAPTION_VISUAL_PRESETS[preset_id]
