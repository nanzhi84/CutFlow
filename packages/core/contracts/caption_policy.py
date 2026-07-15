"""Shared fixed-caption-band policy loaded from the cross-client JSON source."""

from __future__ import annotations

import json
from pathlib import Path

_POLICY = json.loads(
    Path(__file__).with_name("caption_policy.json").read_text(encoding="utf-8")
)

CAPTION_ANCHOR_X = float(_POLICY["anchor_x"])
CAPTION_BASELINE_Y = float(_POLICY["baseline_y"])
CAPTION_LINE_HEIGHT_RATIO = float(_POLICY["line_height_ratio"])
CAPTION_MAX_WIDTH_RATIO = float(_POLICY["max_width_ratio"])
