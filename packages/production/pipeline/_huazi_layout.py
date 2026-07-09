"""Deterministic huazi (emphasis caption) layout box generator.

Produces the candidate placement boxes a huazi planning agent chooses from. This
is the position source of truth for huazi and supersedes the static 7-anchor
whitelist in ``_caption_styles.HUAZI_PLACEMENTS`` (kept only for legacy
placement_id rendering). Pure function, no IO, no randomness.

The grid is 5 vertical bands x 3 horizontal anchors x 3 width tiers. Boxes whose
estimated text width exceeds their capacity, or that intrude into the normal
caption safety zone, are dropped. Survivors are ranked by a static collision
score (lower = safer) and capped at 24 candidates.
"""

from __future__ import annotations

import unicodedata

# Vertical bands as (name, y_top, y_bottom) normalized to canvas height. Each
# band cell is one huazi line tall (huazi max_lines=1); the box occupies the
# full band range and text renders vertically centered inside it.
_BANDS: tuple[tuple[str, float, float], ...] = (
    ("top", 0.06, 0.14),
    ("upper", 0.16, 0.30),
    ("middle", 0.34, 0.48),
    ("lower_middle", 0.52, 0.62),
    ("lower", 0.64, 0.72),
)

_ANCHORS: tuple[str, ...] = ("left", "center", "right")

# Width tiers as (name, width_frac, text_capacity). Capacity is the number of
# full-width chars that fit, ~= floor(width / 0.04 normalized full-width advance).
_CAPACITY_TIERS: tuple[tuple[str, float, int], ...] = (
    ("compact", 0.30, 7),
    ("medium", 0.44, 11),
    ("wide", 0.62, 15),
)

# Horizontal inset for edge-anchored boxes (left/right), normalized to width.
_EDGE_INSET = 0.04

# Safety gap kept below a box before the normal caption zone (normalized).
_SAFETY_MARGIN = 0.02

# Extra collision penalty for a box already used by a neighbouring event.
_NEIGHBOR_PENALTY = 0.30

# Cap on emitted candidates (spec target band is 12-24).
_MAX_CANDIDATES = 24

# Static collision priors for centered boxes. middle-center overlaps the digital
# human face, so it is the most dangerous placement.
_CENTER_COLLISION: dict[str, float] = {
    "top": 0.10,
    "upper": 0.10,
    "middle": 0.60,
    "lower_middle": 0.35,
    "lower": 0.35,
}

# Static collision priors for edge-anchored boxes, rising top -> bottom (the
# lower a side box sits, the more it competes with body/captions).
_SIDE_COLLISION: dict[str, float] = {
    "top": 0.05,
    "upper": 0.08,
    "middle": 0.12,
    "lower_middle": 0.16,
    "lower": 0.20,
}

_BAND_ORDER = {name: index for index, (name, _low, _high) in enumerate(_BANDS)}
_ANCHOR_ORDER = {name: index for index, name in enumerate(_ANCHORS)}
_CAPACITY_ORDER = {name: index for index, (name, _w, _c) in enumerate(_CAPACITY_TIERS)}


def _char_units(char: str) -> float:
    """Coarse EAW width in full-width-char units (aligned with _font_metrics)."""
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 1.0
    if char.isspace():
        return 0.35
    return 0.5


def _estimate_units(text: str) -> float:
    return sum(_char_units(char) for char in str(text))


def _anchor_x(anchor: str, width: float) -> float:
    if anchor == "left":
        return _EDGE_INSET
    if anchor == "right":
        return 1.0 - _EDGE_INSET - width
    return (1.0 - width) / 2.0


def _enter_directions(band: str, anchor: str) -> list[str]:
    if anchor == "left":
        return ["left", "up"]
    if anchor == "right":
        return ["right", "up"]
    # center: a top-band box slides from nowhere (fade/pop only); elsewhere it
    # rises into place.
    if band == "top":
        return []
    return ["up"]


def _collision_score(band: str, anchor: str, in_neighbors: bool) -> float:
    base = _CENTER_COLLISION[band] if anchor == "center" else _SIDE_COLLISION[band]
    if in_neighbors:
        base += _NEIGHBOR_PENALTY
    return round(base, 4)


def generate_layout_boxes(
    *,
    event_text: str,
    resolution: tuple[int, int],
    normal_caption_top_y: float,
    neighbor_boxes: list[str],
) -> list[dict]:
    """Build the deterministic huazi candidate boxes for one emphasis event.

    ``resolution`` is part of the stable interface (the caller carries the canvas
    size for downstream pixel materialization); the v1 static grid is fully
    normalized and does not consume it. ``normal_caption_top_y`` is the normalized
    top edge of the normal caption safety zone (worst-case 2 lines). Boxes that
    reach within ``_SAFETY_MARGIN`` of it, or that cannot hold ``event_text``, are
    dropped. Returns 12-24 candidates ranked by ascending collision score; fewer
    than 12 is possible (and honestly reported) when the safety zone is high.
    """
    del resolution  # reserved for future aspect-aware refinement

    neighbor_set = set(neighbor_boxes or ())
    estimated_units = _estimate_units(event_text)
    safety_limit = normal_caption_top_y - _SAFETY_MARGIN

    scored: list[tuple[tuple[float, int, int, int], dict]] = []
    for band, y_low, y_high in _BANDS:
        height = round(y_high - y_low, 4)
        if y_low + height > safety_limit:
            continue
        for anchor in _ANCHORS:
            directions = _enter_directions(band, anchor)
            for capacity_name, width, capacity in _CAPACITY_TIERS:
                if estimated_units > capacity:
                    continue
                box_id = f"{band}_{anchor}_{capacity_name}"
                score = _collision_score(band, anchor, box_id in neighbor_set)
                box = {
                    "layout_box_id": box_id,
                    "rect": {
                        "x": round(_anchor_x(anchor, width), 4),
                        "y": round(y_low, 4),
                        "w": round(width, 4),
                        "h": height,
                    },
                    "text_align": anchor,
                    "max_lines": 1,
                    "text_capacity": capacity,
                    "allowed_enter_directions": list(directions),
                    "collision_score": score,
                    "region_tags": [band, anchor],
                }
                sort_key = (
                    score,
                    _BAND_ORDER[band],
                    _ANCHOR_ORDER[anchor],
                    _CAPACITY_ORDER[capacity_name],
                )
                scored.append((sort_key, box))

    scored.sort(key=lambda item: item[0])
    return [box for _key, box in scored[:_MAX_CANDIDATES]]
