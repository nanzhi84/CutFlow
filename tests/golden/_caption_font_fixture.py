from __future__ import annotations

import tempfile
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from packages.core.contracts import ArtifactKind, MediaAssetRecord, MediaInfo
from packages.core.storage.database import ArtifactRow, MediaAssetRow
from packages.core.storage.repository import new_id
from packages.media.assets import store_file
from packages.production.pipeline._fonts import (
    DEFAULT_EMPHASIS_FONT_ASSET_ID,
    DEFAULT_NORMAL_FONT_ASSET_ID,
)


_FONT_DIR = Path(tempfile.gettempdir()) / "cutagent-golden-caption-fonts"


def register_default_caption_fonts(app) -> None:
    """Give full-chain golden runs deterministic, offline caption font assets."""

    register_caption_fonts(app.state.repository, app.state.object_store)


def register_caption_fonts(repository, object_store) -> None:
    """Register the deterministic test pair on an in-memory workflow repository."""

    for asset_id, family in _font_specs():
        path = _font_path(asset_id, family)
        stored = store_file(object_store, path, purpose="golden-caption-fonts")
        artifact = repository.create_artifact(
            kind=ArtifactKind.uploaded_file,
            payload_schema="uri-only",
            payload=None,
            uri=stored.ref.uri,
            sha256=stored.sha256,
            size_bytes=path.stat().st_size,
            media_info=MediaInfo(media_type="json", codec="font/ttf", format="ttf"),
        )
        repository.media_assets[asset_id] = MediaAssetRecord(
            id=asset_id,
            title=family,
            kind="font",
            source_artifact_id=artifact.id,
            usable=True,
        )


def register_sql_caption_fonts(session_factory, object_store) -> None:
    """Persist the deterministic pair for SQL/Temporal full-chain tests."""

    with session_factory() as session:
        for asset_id, family in _font_specs():
            if session.get(MediaAssetRow, asset_id) is not None:
                continue
            path = _font_path(asset_id, family)
            stored = store_file(object_store, path, purpose="golden-caption-fonts")
            artifact_id = new_id("art")
            session.add(
                ArtifactRow(
                    id=artifact_id,
                    kind=ArtifactKind.uploaded_file.value,
                    uri=stored.ref.uri,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    media_info=MediaInfo(
                        media_type="json", codec="font/ttf", format="ttf"
                    ).model_dump(mode="json"),
                    payload_schema="UploadedFileArtifact.v1",
                    payload={
                        "filename": path.name,
                        "content_type": "font/ttf",
                        "size_bytes": stored.size_bytes,
                        "object_uri": stored.ref.uri,
                        "sha256": stored.sha256,
                        "metadata": {"test_fixture": "caption-font", "asset_id": asset_id},
                    },
                )
            )
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    case_id=None,
                    title=family,
                    kind="font",
                    source_artifact_id=artifact_id,
                    tags=["caption-font", "test-fixture"],
                    annotation_status="annotated",
                    usable=True,
                )
            )
        session.commit()


def _font_specs() -> tuple[tuple[str, str], ...]:
    return (
        (DEFAULT_NORMAL_FONT_ASSET_ID, "Cutagent Golden Serif"),
        (DEFAULT_EMPHASIS_FONT_ASSET_ID, "Cutagent Golden Sans"),
    )


def _font_path(asset_id: str, family: str) -> Path:
    path = _FONT_DIR / f"{asset_id}.ttf"
    if not path.exists():
        _build_font(path, family)
    return path


def _build_font(path: Path, family: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    glyph_order = [".notdef", "glyph"]
    empty = TTGlyphPen(None).glyph()
    font = FontBuilder(1000, isTTF=True)
    font.setupGlyphOrder(glyph_order)
    codepoints = range(0x20, 0xA0)
    cjk_codepoints = range(0x3000, 0xA000)
    font.setupCharacterMap({codepoint: "glyph" for codepoint in (*codepoints, *cjk_codepoints)})
    font.setupGlyf({name: empty for name in glyph_order})
    font.setupHorizontalMetrics({".notdef": (600, 0), "glyph": (600, 0)})
    font.setupHorizontalHeader(ascent=800, descent=-200)
    font.setupNameTable({"familyName": family, "styleName": "Regular"})
    font.setupOS2(sTypoAscender=800, sTypoDescender=-200)
    font.setupPost()
    font.save(str(path))
