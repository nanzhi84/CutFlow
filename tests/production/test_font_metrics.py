"""Unit tests for the font advance-width metrics module."""

from __future__ import annotations

from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from packages.production.pipeline._font_metrics import (
    FontMetrics,
    char_advance_px,
    fallback_char_px,
    font_text_safety_issue,
    font_text_safety_report,
    load_font_metrics,
    make_text_measurer,
)

# Known geometry baked into the synthetic font below.
_UPEM = 1000
_ASCENDER = 800
_DESCENDER = -200  # cell height == ascender - descender == 1000
_ADVANCE_ASCII = 500  # glyph "A" (U+0041)
_ADVANCE_CJK = 1000  # glyph "cjk" (U+4E00 一)
_FONT_SIZE = 48.0


def _build_test_font(path: Path) -> None:
    """Build a minimal TTF with known upem/hhea and two mapped glyphs."""
    glyph_order = [".notdef", "A", "cjk"]
    empty = TTGlyphPen(None).glyph()

    fb = FontBuilder(_UPEM, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap({0x41: "A", 0x4E00: "cjk"})
    fb.setupGlyf({name: empty for name in glyph_order})
    fb.setupHorizontalMetrics(
        {".notdef": (600, 0), "A": (_ADVANCE_ASCII, 0), "cjk": (_ADVANCE_CJK, 0)}
    )
    fb.setupHorizontalHeader(ascent=_ASCENDER, descent=_DESCENDER)
    fb.setupNameTable({"familyName": "MetricsTest", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=_ASCENDER, sTypoDescender=_DESCENDER)
    fb.setupPost()
    fb.save(str(path))


@pytest.fixture
def font_path(tmp_path: Path) -> Path:
    path = tmp_path / "metrics-test.ttf"
    _build_test_font(path)
    return path


def test_load_font_metrics_reads_head_and_hhea(font_path: Path) -> None:
    metrics = load_font_metrics(font_path)
    assert metrics is not None
    assert metrics.upem == _UPEM
    assert metrics.ascender == _ASCENDER
    assert metrics.descender == _DESCENDER
    assert metrics.cell_height == 1000


def test_char_advance_px_uses_cell_height_denominator(font_path: Path) -> None:
    metrics = load_font_metrics(font_path)
    assert metrics is not None
    # advance_units * font_size / (ascender - descender)
    assert char_advance_px(metrics, "A", _FONT_SIZE) == pytest.approx(24.0)  # 500*48/1000
    assert char_advance_px(metrics, "一", _FONT_SIZE) == pytest.approx(48.0)  # 1000*48/1000


def test_char_advance_px_cmap_miss_reserves_full_rendered_cell(font_path: Path) -> None:
    metrics = load_font_metrics(font_path)
    assert metrics is not None
    # 丁 (U+4E01) is not in the cmap -> one full rendered cell.
    assert char_advance_px(metrics, "丁", _FONT_SIZE) == pytest.approx(48.0)
    assert char_advance_px(metrics, "", _FONT_SIZE) == 0.0

    tall_cell_metrics = FontMetrics(
        upem=1000,
        ascender=1000,
        descender=-600,
        cmap={},
        advances={},
    )
    assert char_advance_px(tall_cell_metrics, "W", _FONT_SIZE) == pytest.approx(48.0)


def test_font_text_safety_rejects_missing_glyph(font_path: Path) -> None:
    report = font_text_safety_report(font_path, ["丁"])

    assert font_text_safety_issue(font_path, ["丁"]) == "missing_glyph:U+4E01"
    assert report.missing_codepoints == (0x4E01,)
    assert report.blocking_issue(allow_missing_glyphs=True) is None


def test_font_text_safety_reports_horizontal_ink_overhang_as_layout_margin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "overhang.ttf"
    pen = TTGlyphPen(None)
    pen.moveTo((-100, 0))
    pen.lineTo((400, 0))
    pen.lineTo((400, 700))
    pen.lineTo((-100, 700))
    pen.closePath()
    glyph = pen.glyph()
    empty = TTGlyphPen(None).glyph()
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "f"])
    fb.setupCharacterMap({ord("f"): "f"})
    fb.setupGlyf({".notdef": empty, "f": glyph})
    fb.setupHorizontalMetrics({".notdef": (600, 0), "f": (300, -100)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Overhang Test", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200)
    fb.setupPost()
    fb.save(str(path))

    report = font_text_safety_report(path, ["f"])

    assert font_text_safety_issue(path, ["f"]) is None
    assert report.horizontal_left_overhang_units == 100
    assert report.horizontal_right_overhang_units == 100
    assert report.horizontal_overhang_px(50) == pytest.approx(10)


def test_load_font_metrics_returns_none_on_corrupt_file(tmp_path: Path) -> None:
    corrupt = tmp_path / "broken.ttf"
    corrupt.write_bytes(b"not a real font at all")
    assert load_font_metrics(corrupt) is None


def test_load_font_metrics_returns_none_on_missing_path(tmp_path: Path) -> None:
    assert load_font_metrics(tmp_path / "does-not-exist.ttf") is None


def test_fallback_char_px_locked_values() -> None:
    assert fallback_char_px("一", _FONT_SIZE) == pytest.approx(36.0)  # wide
    assert fallback_char_px("A", _FONT_SIZE) == pytest.approx(18.0)  # half-width
    assert fallback_char_px(" ", _FONT_SIZE) == pytest.approx(12.48)  # whitespace
    assert fallback_char_px("", _FONT_SIZE) == 0.0


def test_make_text_measurer_hmtx_path(font_path: Path) -> None:
    metrics = load_font_metrics(font_path)
    measure, source = make_text_measurer(metrics, _FONT_SIZE)
    assert source == "hmtx"
    assert measure("A一") == pytest.approx(72.0)  # 24 + 48


def test_make_text_measurer_eaw_fallback_path() -> None:
    measure, source = make_text_measurer(None, _FONT_SIZE)
    assert source == "eaw_fallback"
    assert measure("A一") == pytest.approx(54.0)  # 18 + 36
