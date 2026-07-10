"""Font advance-width metrics for deterministic caption line-breaking.

The caption display compiler needs to know how wide each character renders so it
can wrap lines to fit the burn-in box. libass sizes glyphs with
``FT_SIZE_REQUEST_TYPE_REAL_DIM``: the requested ``fontsize`` is the *cell
height* (ascender − descender), not the em square. So a glyph's pixel advance is

    advance_units × fontsize / (hhea.ascender − hhea.descender)

and **not** ``advance_units × fontsize / upem``. Getting this wrong biases every
width estimate by the font's typo-vs-cell ratio and desyncs our wrapping from the
actual libass render. When fontTools cannot read the file we fall back to a
coarse East-Asian-Width heuristic (calibrated so full-width ≈ fontsize×0.75, in
line with the frontend ``ASS_FONT_POINT_TO_CSS_PIXEL`` 72/96 factor) and report
the degraded ``metrics_source`` upward.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("packages.production.pipeline._font_metrics")


@dataclass(frozen=True)
class FontMetrics:
    """Advance-width lookup for one font face (fontNumber 0 of a collection)."""

    upem: int
    ascender: int  # hhea.ascender (font units)
    descender: int  # hhea.descender (font units, negative)
    cmap: Mapping[int, str] = field(default_factory=dict, compare=False, repr=False)
    advances: Mapping[str, int] = field(default_factory=dict, compare=False, repr=False)

    @property
    def cell_height(self) -> int:
        return self.ascender - self.descender


def load_font_metrics(font_path: Path) -> FontMetrics | None:
    """Read TTF/OTF/TTC(fontNumber=0) metrics with fontTools.

    Returns ``None`` on any failure (missing file, unreadable/corrupt font,
    missing required tables) so the caller can report a ``font.metrics_fallback``
    degradation and switch to the heuristic measurer. Never raises.
    """
    try:
        from fontTools.ttLib import TTFont
    except Exception:  # fontTools absent or import-time failure
        return None

    try:
        font = TTFont(str(font_path), fontNumber=0, lazy=True)
    except Exception:
        return None

    try:
        upem = int(font["head"].unitsPerEm)
        hhea = font["hhea"]
        ascender = int(hhea.ascender)
        descender = int(hhea.descender)
        try:
            cmap: dict[int, str] = dict(font["cmap"].getBestCmap() or {})
        except Exception:
            cmap = {}
        hmtx = font["hmtx"]
        advances = {name: int(hmtx[name][0]) for name in font.getGlyphOrder()}
    except Exception as exc:  # missing table / malformed metrics
        logger.warning("[font_metrics] could not read %s: %s", font_path, exc)
        return None
    finally:
        font.close()

    if upem <= 0 or ascender - descender <= 0:
        return None
    return FontMetrics(
        upem=upem,
        ascender=ascender,
        descender=descender,
        cmap=cmap,
        advances=advances,
    )


def char_advance_px(metrics: FontMetrics, char: str, font_size: float) -> float:
    """Pixel advance of ``char`` at ``font_size`` using the libass cell-height rule.

    Aligns with libass ``FT_SIZE_REQUEST_TYPE_REAL_DIM`` (cell-height == fontsize);
    do not "fix" the denominator to ``upem``. Characters absent from the cmap fall
    back to a full-width approximation (advance == upem).
    """
    if not char:
        return 0.0
    glyph = metrics.cmap.get(ord(char))
    advance = metrics.advances.get(glyph) if glyph is not None else None
    if advance is None:
        advance = metrics.upem
    return advance * font_size / metrics.cell_height


def fallback_char_px(char: str, font_size: float) -> float:
    """EAW heuristic width when no readable font metrics are available.

    Full/wide (EAW F/W) ≈ fontsize×0.75, whitespace ≈ ×0.26, other half-width
    ≈ ×0.375. The 0.75 factor matches the frontend's measured
    ``ASS_FONT_POINT_TO_CSS_PIXEL`` (72/96) calibration.
    """
    if not char:
        return 0.0
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return font_size * 0.75
    if char.isspace():
        return font_size * 0.26
    return font_size * 0.375


def make_text_measurer(
    metrics: FontMetrics | None, font_size: float
) -> tuple[Callable[[str], float], str]:
    """Build a per-character summing text measurer and its source label.

    ``source`` is ``"hmtx"`` when real font metrics drive the width and
    ``"eaw_fallback"`` when the EAW heuristic does.
    """
    if metrics is None:

        def measure_fallback(text: str) -> float:
            return sum(fallback_char_px(ch, font_size) for ch in text)

        return measure_fallback, "eaw_fallback"

    def measure_hmtx(text: str) -> float:
        return sum(char_advance_px(metrics, ch, font_size) for ch in text)

    return measure_hmtx, "hmtx"
