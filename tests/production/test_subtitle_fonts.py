from __future__ import annotations

import struct
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from packages.production.pipeline._fonts import (
    DEFAULT_FONT_SENTINEL,
    ResolvedFont,
    _decode_name_record,
    _read_family_builtin,
    _stage_sfnt_font,
    caption_font_asset_ids,
    distinct_font_assets_share_family,
    is_font_collection,
    resolve_font_asset,
    resolve_subtitle_font,
)


def _build_font(path: Path, family: str = "Caption Test") -> None:
    glyph_order = [".notdef", "A"]
    empty = TTGlyphPen(None).glyph()
    font = FontBuilder(1000, isTTF=True)
    font.setupGlyphOrder(glyph_order)
    font.setupCharacterMap({0x41: "A"})
    font.setupGlyf({name: empty for name in glyph_order})
    font.setupHorizontalMetrics({".notdef": (600, 0), "A": (500, 0)})
    font.setupHorizontalHeader(ascent=800, descent=-200)
    font.setupNameTable({"familyName": family, "styleName": "Regular"})
    font.setupOS2(sTypoAscender=800, sTypoDescender=-200)
    font.setupPost()
    font.save(str(path))


def _build_name_only_font(family: str) -> bytes:
    encoded = family.encode("utf-16-be")
    string_offset = 18
    name_table = (
        struct.pack(">HHH", 0, 1, string_offset)
        + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(encoded), 0)
        + encoded
    )
    header = struct.pack(">4sHHHH", b"\x00\x01\x00\x00", 1, 0, 0, 0)
    entry = struct.pack(">4sIII", b"name", 0, 28, len(name_table))
    return header + entry + name_table


def test_caption_font_pair_defaults_and_explicit_selection() -> None:
    assert caption_font_asset_ids(None, None) == (
        "asset_font_noto_serif_cjk_sc_regular",
        "asset_font_noto_sans_cjk_sc_bold",
    )
    assert caption_font_asset_ids(DEFAULT_FONT_SENTINEL, DEFAULT_FONT_SENTINEL) == (
        "asset_font_noto_serif_cjk_sc_regular",
        "asset_font_noto_sans_cjk_sc_bold",
    )
    assert caption_font_asset_ids("font_brand", None) == ("font_brand", "font_brand")
    assert caption_font_asset_ids("font_body", "font_display") == (
        "font_body",
        "font_display",
    )


def test_distinct_font_assets_detect_ambiguous_family_and_collections(tmp_path) -> None:
    shared = ResolvedFont("Shared Family", tmp_path, tmp_path / "body.ttf")
    same = ResolvedFont("shared family", tmp_path, tmp_path / "display.ttf")
    other = ResolvedFont("Other Family", tmp_path, tmp_path / "display.otf")
    collection = ResolvedFont("Collection", tmp_path, tmp_path / "display.ttc")

    assert distinct_font_assets_share_family("body", shared, "display", same) is True
    assert distinct_font_assets_share_family("body", shared, "body", same) is False
    assert distinct_font_assets_share_family("body", shared, "display", other) is False
    assert distinct_font_assets_share_family("body", shared, "display", None) is False
    assert is_font_collection(collection) is True
    assert is_font_collection(other) is False
    assert is_font_collection(None) is False


def test_resolve_subtitle_font_stages_content_addressed_native_font(tmp_path) -> None:
    source = tmp_path / "brand.ttf"
    _build_font(source, "Brand Sans")
    runtime = tmp_path / "runtime"

    first = resolve_subtitle_font(font_path=source, runtime_dir=runtime)
    second = resolve_subtitle_font(font_path=source, runtime_dir=runtime)

    assert first is not None and second is not None
    assert first.family_name == "Brand Sans"
    assert first.source_path == second.source_path
    assert first.source_path.name.startswith("font-")
    assert first.source_path.read_bytes() == source.read_bytes()


def test_same_named_fonts_remain_distinct_by_content(tmp_path) -> None:
    normal = tmp_path / "normal" / "font.ttf"
    emphasis = tmp_path / "emphasis" / "font.ttf"
    normal.parent.mkdir()
    emphasis.parent.mkdir()
    _build_font(normal, "Body Family")
    _build_font(emphasis, "Display Family")
    runtime = tmp_path / "runtime"

    resolved_normal = resolve_subtitle_font(font_path=normal, runtime_dir=runtime)
    resolved_emphasis = resolve_subtitle_font(font_path=emphasis, runtime_dir=runtime)

    assert resolved_normal is not None and resolved_emphasis is not None
    assert resolved_normal.source_path != resolved_emphasis.source_path
    assert {path.name for path in runtime.iterdir()} == {
        resolved_normal.source_path.name,
        resolved_emphasis.source_path.name,
    }


@pytest.mark.parametrize("flavor", ["woff", "woff2"])
def test_resolve_subtitle_font_converts_web_font_to_native(tmp_path, flavor: str) -> None:
    if flavor == "woff2":
        pytest.importorskip("brotli")
    source = tmp_path / "source.ttf"
    web = tmp_path / f"brand.{flavor}"
    _build_font(source, "Web Caption")
    font = TTFont(str(source))
    try:
        font.flavor = flavor
        font.save(str(web))
    finally:
        font.close()

    resolved = resolve_subtitle_font(font_path=web, runtime_dir=tmp_path / "runtime")

    assert resolved is not None
    assert resolved.family_name == "Web Caption"
    assert resolved.source_path.suffix == ".ttf"
    assert resolved.source_path.read_bytes()[:4] not in {b"wOFF", b"wOF2"}


def test_resolve_subtitle_font_rejects_missing_nonfont_and_corrupt(tmp_path) -> None:
    assert (
        resolve_subtitle_font(font_path=tmp_path / "missing.ttf", runtime_dir=tmp_path / "runtime")
        is None
    )
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG")
    assert resolve_subtitle_font(font_path=image, runtime_dir=tmp_path / "runtime") is None
    corrupt = tmp_path / "corrupt.ttf"
    corrupt.write_bytes(b"not a font")
    assert resolve_subtitle_font(font_path=corrupt, runtime_dir=tmp_path / "runtime") is None


def test_resolve_subtitle_font_rejects_missing_or_unsafe_family(tmp_path) -> None:
    nameless = tmp_path / "nameless.ttf"
    _build_font(nameless)
    font = TTFont(str(nameless))
    try:
        del font["name"]
        font.save(str(nameless))
    finally:
        font.close()
    assert resolve_subtitle_font(font_path=nameless, runtime_dir=tmp_path / "runtime") is None

    unsafe = tmp_path / "unsafe.ttf"
    _build_font(unsafe, "Unsafe, Family")
    assert resolve_subtitle_font(font_path=unsafe, runtime_dir=tmp_path / "runtime") is None


def test_resolve_font_asset_reports_missing_and_unstageable_assets(tmp_path) -> None:
    assert resolve_font_asset(
        font_asset_id=None,
        runtime_dir=tmp_path / "runtime",
        source_artifact_for_asset=lambda _asset_id: object(),
        artifact_path=lambda _artifact: tmp_path / "font.ttf",
    ) == (None, None)
    assert resolve_font_asset(
        font_asset_id=DEFAULT_FONT_SENTINEL,
        runtime_dir=tmp_path / "runtime",
        source_artifact_for_asset=lambda _asset_id: object(),
        artifact_path=lambda _artifact: tmp_path / "font.ttf",
    ) == (None, None)
    assert resolve_font_asset(
        font_asset_id="missing",
        runtime_dir=tmp_path / "runtime",
        source_artifact_for_asset=lambda _asset_id: (_ for _ in ()).throw(KeyError()),
        artifact_path=lambda _artifact: tmp_path / "font.ttf",
    ) == (None, "missing")

    corrupt = tmp_path / "bad.woff"
    corrupt.write_bytes(b"not a web font")
    resolved, unresolved = resolve_font_asset(
        font_asset_id="bad",
        runtime_dir=tmp_path / "runtime",
        source_artifact_for_asset=lambda _asset_id: object(),
        artifact_path=lambda _artifact: corrupt,
    )
    assert resolved is None
    assert unresolved == "bad"


def test_resolve_font_asset_returns_staged_font(tmp_path) -> None:
    source = tmp_path / "brand.otf"
    _build_font(source, "Resolved Family")
    resolved, unresolved = resolve_font_asset(
        font_asset_id="brand",
        runtime_dir=tmp_path / "runtime",
        source_artifact_for_asset=lambda _asset_id: object(),
        artifact_path=lambda _artifact: source,
    )

    assert unresolved is None
    assert resolved is not None
    assert resolved.family_name == "Resolved Family"


def test_builtin_family_parser_and_name_record_decoders(tmp_path) -> None:
    source = tmp_path / "name-only.ttf"
    source.write_bytes(_build_name_only_font("Builtin Family"))

    assert _read_family_builtin(source) == "Builtin Family"
    assert _read_family_builtin(tmp_path / "missing.ttf") is None
    short = tmp_path / "short.ttf"
    short.write_bytes(b"short")
    assert _read_family_builtin(short) is None
    wrong = tmp_path / "wrong.ttf"
    wrong.write_bytes(b"BAD!" + b"\0" * 32)
    assert _read_family_builtin(wrong) is None
    no_name = tmp_path / "no-name.ttf"
    no_name.write_bytes(struct.pack(">4sHHHH", b"OTTO", 0, 0, 0, 0))
    assert _read_family_builtin(no_name) is None

    assert _decode_name_record(3, "中文".encode("utf-16-be")) == "中文"
    assert _decode_name_record(1, b"Mac Family") == "Mac Family"
    assert _decode_name_record(9, b"\x00O\x00t\x00h\x00e\x00r") == "Other"
    assert _decode_name_record(3, b"") is None
    assert _decode_name_record(3, b"\xff") is None


def test_stage_native_font_returns_none_when_source_cannot_be_fingerprinted(tmp_path) -> None:
    assert _stage_sfnt_font(tmp_path / "missing.ttf", tmp_path / "runtime") is None
