"""Caption animation registry shared by ASS rendering, layout, and SFX planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from packages.core.contracts.caption_effects_policy import (
    CAPTION_EFFECT_IDS,
    caption_effect_roles,
)


@dataclass(frozen=True)
class CaptionEffectRenderContext:
    text: str
    x: float
    y: float
    start_ms: float
    end_ms: float
    frame_duration_ms: float
    char_enter_ms: tuple[int, ...] | None = None
    char_advances_px: tuple[float, ...] | None = None


@dataclass(frozen=True)
class CaptionEffectFragment:
    text: str
    start_ms: float
    end_ms: float
    tags: tuple[str, ...]


@dataclass(frozen=True)
class CaptionEffectSpec:
    effect_id: str
    roles: frozenset[str]
    enter_ms: int
    headroom_px_ratio: float
    fixed_left_headroom_px: float
    needs_char_timing: bool
    sfx_class: str | None
    _renderer: Callable[[CaptionEffectRenderContext], list[CaptionEffectFragment]] = field(
        repr=False,
        compare=False,
    )

    def render(self, context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
        if self.needs_char_timing and (
            context.char_enter_ms is None
            or context.char_advances_px is None
            or len(context.char_enter_ms) != len(context.text)
            or len(context.char_advances_px) != len(context.text)
        ):
            raise ValueError(f"{self.effect_id} requires per-character timing and advances")
        return self._renderer(context)

    def headroom_px(self, advance_px: float) -> float:
        left, right = self.headroom_sides_px(advance_px)
        return left + right

    def headroom_sides_px(self, advance_px: float) -> tuple[float, float]:
        advance = max(0.0, float(advance_px))
        if advance == 0:
            return (0.0, 0.0)
        return (
            self.fixed_left_headroom_px,
            advance * self.headroom_px_ratio,
        )


def _fragment(
    context: CaptionEffectRenderContext,
    *tags: str,
) -> list[CaptionEffectFragment]:
    return [
        CaptionEffectFragment(
            text=context.text,
            start_ms=context.start_ms,
            end_ms=context.end_ms,
            tags=("\\an7", *tags),
        )
    ]


def _none(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    return _fragment(context, f"\\pos({round(context.x)},{round(context.y)})")


def _soft_in(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    return _fragment(
        context,
        f"\\pos({round(context.x)},{round(context.y)})",
        "\\fad(120,0)",
    )


def _fade_through(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    return _fragment(
        context,
        f"\\pos({round(context.x)},{round(context.y)})",
        "\\fad(120,100)",
    )


def _wipe_reveal(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    assert context.char_enter_ms is not None
    assert context.char_advances_px is not None
    fragments: list[CaptionEffectFragment] = []
    x = context.x
    latest_start_ms = max(
        context.start_ms,
        context.end_ms - 1.5 * context.frame_duration_ms,
    )
    for char, start_ms, advance_px in zip(
        context.text,
        context.char_enter_ms,
        context.char_advances_px,
        strict=True,
    ):
        cx = round(x)
        fragments.append(
            CaptionEffectFragment(
                text=char,
                start_ms=min(max(context.start_ms, start_ms), latest_start_ms),
                end_ms=context.end_ms,
                tags=(
                    "\\an7",
                    "\\fad(60,0)",
                    f"\\move({cx - 14},{round(context.y)},{cx},{round(context.y)},0,90)",
                ),
            )
        )
        x += advance_px
    return fragments


def _slide_up_in(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    x = round(context.x)
    y = round(context.y)
    return _fragment(context, f"\\move({x},{y + 22},{x},{y},0,160)", "\\fad(90,0)")


def _pop(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    return _fragment(
        context,
        f"\\pos({round(context.x)},{round(context.y)})",
        "\\fscx85\\fscy85",
        "\\t(0,120,\\fscx105\\fscy105)",
        "\\t(120,240,\\fscx100\\fscy100)",
    )


def _pop_rotate(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    return _fragment(
        context,
        f"\\pos({round(context.x)},{round(context.y)})",
        "\\frz-6\\fscx80\\fscy80",
        "\\t(0,140,\\frz0\\fscx108\\fscy108)",
        "\\t(140,260,\\fscx100\\fscy100)",
    )


def _jelly_pop(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    return _fragment(
        context,
        f"\\pos({round(context.x)},{round(context.y)})",
        "\\fscx85\\fscy85",
        "\\t(0,120,\\fscx105\\fscy105)",
        "\\t(120,240,\\fscx100\\fscy100)",
        "\\t(240,420,\\fscx97\\fscy103)",
        "\\t(420,600,\\fscx101\\fscy99)",
        "\\t(600,780,\\fscx100\\fscy100)",
    )


def _drop_in(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    x = round(context.x)
    y = round(context.y)
    return _fragment(
        context,
        f"\\move({x},{y - 26},{x},{y},0,140)",
        "\\fad(60,0)",
        "\\t(140,200,\\fscy94)",
        "\\t(200,260,\\fscy100)",
    )


def _zoom_settle(context: CaptionEffectRenderContext) -> list[CaptionEffectFragment]:
    return _fragment(
        context,
        f"\\pos({round(context.x)},{round(context.y)})",
        "\\fscx130\\fscy130",
        "\\fad(80,0)",
        "\\t(0,200,\\fscx100\\fscy100)",
    )


def _spec(
    effect_id: str,
    *,
    enter_ms: int,
    headroom_px_ratio: float = 0.0,
    fixed_left_headroom_px: float = 0.0,
    needs_char_timing: bool = False,
    sfx_class: str | None = None,
    renderer: Callable[[CaptionEffectRenderContext], list[CaptionEffectFragment]],
) -> CaptionEffectSpec:
    return CaptionEffectSpec(
        effect_id=effect_id,
        roles=caption_effect_roles(effect_id),
        enter_ms=enter_ms,
        headroom_px_ratio=headroom_px_ratio,
        fixed_left_headroom_px=fixed_left_headroom_px,
        needs_char_timing=needs_char_timing,
        sfx_class=sfx_class,
        _renderer=renderer,
    )


_EFFECTS = {
    "none": _spec("none", enter_ms=0, renderer=_none),
    "soft_in": _spec("soft_in", enter_ms=120, renderer=_soft_in),
    "fade_through": _spec("fade_through", enter_ms=120, renderer=_fade_through),
    "wipe_reveal": _spec(
        "wipe_reveal",
        enter_ms=90,
        fixed_left_headroom_px=14,
        needs_char_timing=True,
        sfx_class="whoosh",
        renderer=_wipe_reveal,
    ),
    "slide_up_in": _spec(
        "slide_up_in",
        enter_ms=160,
        sfx_class="whoosh",
        renderer=_slide_up_in,
    ),
    "pop": _spec(
        "pop",
        enter_ms=240,
        headroom_px_ratio=0.05,
        sfx_class="pop",
        renderer=_pop,
    ),
    "pop_rotate": _spec(
        "pop_rotate",
        enter_ms=260,
        headroom_px_ratio=0.08,
        sfx_class="impact",
        renderer=_pop_rotate,
    ),
    "jelly_pop": _spec(
        "jelly_pop",
        enter_ms=780,
        headroom_px_ratio=0.05,
        sfx_class="pop",
        renderer=_jelly_pop,
    ),
    "drop_in": _spec(
        "drop_in",
        enter_ms=260,
        sfx_class="impact",
        renderer=_drop_in,
    ),
    "zoom_settle": _spec(
        "zoom_settle",
        enter_ms=200,
        headroom_px_ratio=0.30,
        sfx_class="ding",
        renderer=_zoom_settle,
    ),
}

if frozenset(_EFFECTS) != CAPTION_EFFECT_IDS:
    raise RuntimeError("caption effect registry and shared role policy are out of sync")

CAPTION_EFFECTS: Mapping[str, CaptionEffectSpec] = MappingProxyType(_EFFECTS)
ANIMATED_CAPTION_EFFECT_IDS = tuple(effect_id for effect_id in _EFFECTS if effect_id != "none")


def caption_effect(effect_id: str) -> CaptionEffectSpec:
    try:
        return CAPTION_EFFECTS[effect_id]
    except KeyError as exc:
        raise ValueError(f"unknown caption effect: {effect_id}") from exc
