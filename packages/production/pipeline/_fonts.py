"""Resolve a selected subtitle ``font_id`` into a libass-burnable font.

The style plan carries the user/agent-selected ``font_id`` (a media asset
of kind ``font``) all the way to the burn step, but burning only honours it if two
things happen at render time:

1. the uploaded ``.ttf/.otf/.ttc`` is placed where libass can find it -- libass
   only consults fonts in its ``fontsdir`` (plus system fonts), so the upload must
   be copied into a flat runtime directory passed to the ``subtitles`` filter via
   ``:fontsdir=``;
2. the ASS ``Fontname`` is set to the font's *family name* (not the asset id /
   filename) -- libass matches by family, so a wrong/absent family silently falls
   back to the default (Arial).

This module performs both: given the font asset + its local file it builds the
runtime fontsdir and returns the family name to stamp into the ASS style. The
family name is read from the font's ``name`` table (preferring fontTools when it
is installed; otherwise a minimal dependency-free ``name``-table parser that
handles the common TTF/OTF case). When neither yields a name we fall back to the
asset title so the burn still uses a deterministic, human-meaningful family
rather than silently reverting to Arial.

No optional dependency is required: fontTools is used opportunistically and the
module degrades to the built-in parser / asset title when it is absent.
"""

from __future__ import annotations

import logging
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("packages.production.pipeline._fonts")

_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}
DEFAULT_FONT_SENTINEL = "case_default_font"

# OpenType ``name`` table identifiers we care about (family-name records).
_NAME_ID_FAMILY = 1
_NAME_ID_TYPOGRAPHIC_FAMILY = 16  # preferred family (overrides 1 when present)


@dataclass(frozen=True)
class ResolvedFont:
    """A resolved subtitle font ready to burn.

    ``family_name`` goes into the ASS ``Fontname``; ``fonts_dir`` is handed to the
    ffmpeg ``subtitles`` filter as ``:fontsdir=`` so libass can find the file.
    """

    family_name: str
    fonts_dir: Path
    source_path: Path


def resolve_font_asset(
    *,
    font_asset_id: str | None,
    runtime_dir: Path,
    source_artifact_for_asset,
    artifact_path,
    media_assets,
) -> tuple[ResolvedFont | None, str | None]:
    """Resolve and stage a selected font asset for planning or rendering.

    Returns ``(font, unresolved_id)`` so callers can report an explicit
    degradation instead of silently treating an unavailable selected font as the
    default.  The dependency callbacks keep this helper free of ``NodeContext``.
    """

    if not font_asset_id or font_asset_id == DEFAULT_FONT_SENTINEL:
        return None, None
    try:
        font_artifact = source_artifact_for_asset(font_asset_id)
        font_path = artifact_path(font_artifact)
    except Exception:
        return None, font_asset_id
    asset = media_assets.get(font_asset_id)
    fallback_name = getattr(asset, "title", None) if asset is not None else None
    return (
        resolve_subtitle_font(
            font_path=font_path,
            runtime_dir=runtime_dir,
            fallback_name=fallback_name,
        ),
        None,
    )


def resolve_subtitle_font(
    *,
    font_path: Path,
    runtime_dir: Path,
    fallback_name: str | None = None,
) -> ResolvedFont | None:
    """Stage ``font_path`` into ``runtime_dir`` and return its family name.

    Returns ``None`` when the source file is missing / not a font file so callers
    fall back to the default burn (the existing ``font.default_used`` path). The
    runtime directory is created if needed and kept flat (libass matches direct
    font files most reliably).
    """
    source = Path(font_path)
    if not source.exists() or not source.is_file():
        return None
    if source.suffix.lower() not in _FONT_EXTENSIONS:
        logger.warning("[fonts] selected font %s is not a known font file; ignoring", source)
        return None

    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / source.name
    try:
        if not target.exists() or source.stat().st_mtime > target.stat().st_mtime:
            shutil.copy2(source, target)
    except OSError as exc:  # pragma: no cover - filesystem edge
        logger.warning("[fonts] failed to stage font %s -> %s: %s", source, target, exc)
        return None

    family = _read_family_name(target) or (fallback_name or "").strip() or None
    if not family:
        return None
    return ResolvedFont(family_name=family, fonts_dir=runtime_dir, source_path=target)


def _read_family_name(path: Path) -> str | None:
    """Best-effort family name from the font's ``name`` table.

    Prefers fontTools (handles .ttc / exotic encodings); falls back to a minimal
    built-in parser for the common single-font .ttf/.otf case so no optional
    dependency is required.
    """
    name = _read_family_with_fonttools(path)
    if name:
        return name
    return _read_family_builtin(path)


def _read_family_with_fonttools(path: Path) -> str | None:
    try:
        from fontTools.ttLib import TTFont
    except Exception:  # ModuleNotFoundError or import-time failure
        return None
    try:
        font = TTFont(str(path), fontNumber=0, lazy=True)
        try:
            name_table = font["name"]
            for name_id in (_NAME_ID_TYPOGRAPHIC_FAMILY, _NAME_ID_FAMILY):
                record = name_table.getDebugName(name_id)
                if record and record.strip():
                    return record.strip()
        finally:
            font.close()
    except Exception as exc:  # pragma: no cover - corrupt font edge
        logger.warning("[fonts] fontTools could not read %s: %s", path, exc)
    return None


def _read_family_builtin(path: Path) -> str | None:
    """Dependency-free OpenType ``name``-table reader (single-font .ttf/.otf).

    Parses just enough of the sfnt structure to pull a family name. ``.ttc``
    collections and unusual layouts are left to fontTools / the title fallback.
    """
    try:
        data = path.read_bytes()
    except OSError:  # pragma: no cover - filesystem edge
        return None
    if len(data) < 12:
        return None
    sfnt = data[:4]
    if sfnt not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
        return None
    try:
        num_tables = struct.unpack(">H", data[4:6])[0]
        name_offset = name_length = None
        record_base = 12
        for index in range(num_tables):
            entry = record_base + index * 16
            tag = data[entry : entry + 4]
            if tag == b"name":
                name_offset, name_length = struct.unpack(">II", data[entry + 8 : entry + 16])
                break
        if name_offset is None:
            return None
        table = data[name_offset : name_offset + name_length]
        if len(table) < 6:
            return None
        count, string_offset = struct.unpack(">HH", table[2:6])
        candidates: dict[int, str] = {}
        for i in range(count):
            rec = 6 + i * 12
            if rec + 12 > len(table):
                break
            platform_id, _encoding_id, _lang, name_id, length, offset = struct.unpack(
                ">HHHHHH", table[rec : rec + 12]
            )
            if name_id not in (_NAME_ID_FAMILY, _NAME_ID_TYPOGRAPHIC_FAMILY):
                continue
            start = string_offset + offset
            raw = table[start : start + length]
            decoded = _decode_name_record(platform_id, raw)
            if decoded:
                candidates[name_id] = decoded
        return candidates.get(_NAME_ID_TYPOGRAPHIC_FAMILY) or candidates.get(_NAME_ID_FAMILY)
    except (struct.error, IndexError) as exc:  # pragma: no cover - corrupt font edge
        logger.warning("[fonts] builtin parser could not read %s: %s", path, exc)
        return None


def _decode_name_record(platform_id: int, raw: bytes) -> str | None:
    if not raw:
        return None
    # Windows (3) and Unicode (0) platforms store UTF-16BE; Mac (1) typically uses
    # MacRoman for Latin family names. Try the most likely encoding first.
    encodings: list[str] = []
    if platform_id in (0, 3):
        encodings = ["utf-16-be"]
    elif platform_id == 1:
        encodings = ["mac-roman", "latin-1"]
    else:
        encodings = ["utf-16-be", "latin-1"]
    for encoding in encodings:
        try:
            text = raw.decode(encoding).strip()
        except (UnicodeDecodeError, LookupError):
            continue
        if text:
            return text
    return None
