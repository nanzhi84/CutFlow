"""ASS override bodies for the three Caption Liveliness v3 entry effects."""

from __future__ import annotations


CAPTION_V3_EFFECTS = ("soft_in", "pop", "slam_scale")


def normal_reference_geometry(*, height: int) -> tuple[float, float, float]:
    """Return ``(outline, x_shadow, y_shadow)`` in ASS PlayRes pixels.

    The 1080x1920 reference frames use a weak outline and a hard shadow offset
    approximately six pixels right / three pixels down. Scale that envelope for
    other output heights so planning and rendering share the same geometry.
    """

    scale = max(0.5, float(max(1, height)) / 1920.0)
    return round(1.0 * scale, 3), round(6.0 * scale, 3), round(3.0 * scale, 3)


def effect_envelope(effect_id: str) -> tuple[float, float]:
    """Return conservative ``(max_scale, max_vertical_shift_px)``."""

    if effect_id == "soft_in":
        return 1.0, 14.0
    if effect_id == "pop":
        return 1.05, 0.0
    if effect_id == "slam_scale":
        return 2.2, 0.0
    return 1.0, 0.0


def overlay_effect_tags(effect_id: str, *, x: int, y: int) -> list[str]:
    if effect_id == "pop":
        return [
            f"\\pos({x},{y})",
            "\\fscx85\\fscy85",
            "\\t(0,180,\\fscx105\\fscy105)",
            "\\t(180,300,\\fscx100\\fscy100)",
        ]
    if effect_id == "slam_scale":
        return [
            f"\\pos({x},{y})",
            "\\fscx220\\fscy220",
            "\\t(0,240,\\fscx100\\fscy100)",
        ]
    return [f"\\pos({x},{y})"]


def normal_soft_in_tags(*, x: int, y: int, align: int = 2) -> str:
    return f"{{\\an{align}\\move({x},{y + 14},{x},{y},0,140)\\fad(120,0)}}"
