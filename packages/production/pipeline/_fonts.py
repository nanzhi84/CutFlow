"""Resolve a selected subtitle ``font_id`` into a libass-burnable font.

The style plan carries the user/agent-selected ``font_id`` (a media asset
of kind ``font``) all the way to the burn step, but burning only honours it if two
things happen at render time:

1. the uploaded font is materialized as ``.ttf/.otf/.ttc`` where libass can find
   it -- web-font containers (``.woff/.woff2``) are first converted back to an
   sfnt file because libass support for those containers is platform-dependent;
   libass only consults fonts in its ``fontsdir`` (plus system fonts), so the
   upload must be copied into a flat runtime directory passed to the ``subtitles``
   filter via ``:fontsdir=``;
2. the ASS ``Fontname`` is set to the font's *family name* (not the asset id /
   filename) -- libass matches by family, so a wrong/absent family silently falls
   back to the default (Arial).

This module performs both: given the font asset + its local file it builds the
runtime fontsdir and returns the family name to stamp into the ASS style. The
family name is read from the font's ``name`` table. A missing family, corrupt
font, or unconvertible file is rejected and reported by the caller instead of
inventing a name that would make libass silently select a system fallback.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("packages.production.pipeline._fonts")

_SFNT_EXTENSIONS = {".ttf", ".otf", ".ttc"}
_WEB_FONT_EXTENSIONS = {".woff", ".woff2"}
_FONT_EXTENSIONS = _SFNT_EXTENSIONS | _WEB_FONT_EXTENSIONS
DEFAULT_FONT_SENTINEL = "case_default_font"
DEFAULT_NORMAL_FONT_ASSET_ID = "asset_font_noto_serif_cjk_sc_regular"
DEFAULT_EMPHASIS_FONT_ASSET_ID = "asset_font_noto_sans_cjk_sc_bold"

# OpenType ``name`` table identifiers we care about (family-name records).
_NAME_ID_FAMILY = 1


@dataclass(frozen=True)
class ResolvedFont:
    """A resolved subtitle font ready to burn.

    ``family_name`` goes into the ASS ``Fontname``; ``fonts_dir`` is handed to the
    ffmpeg ``subtitles`` filter as ``:fontsdir=`` so libass can find the file.
    """

    family_name: str
    fonts_dir: Path
    source_path: Path


def caption_font_asset_ids(
    normal_font_asset_id: str | None,
    emphasis_font_asset_id: str | None,
) -> tuple[str, str]:
    """Return the deterministic normal/emphasis font pair for a v2 plan.

    A user-selected normal family remains the emphasis fallback unless they
    selected a second family explicitly. With no selection, the versioned
    starter-pack identities reproduce the reference's serif body + bold sans
    emphasis split on every platform.
    """

    explicit_normal = (
        str(normal_font_asset_id).strip()
        if normal_font_asset_id and normal_font_asset_id != DEFAULT_FONT_SENTINEL
        else ""
    )
    explicit_emphasis = (
        str(emphasis_font_asset_id).strip()
        if emphasis_font_asset_id and emphasis_font_asset_id != DEFAULT_FONT_SENTINEL
        else ""
    )
    normal = explicit_normal or DEFAULT_NORMAL_FONT_ASSET_ID
    emphasis = explicit_emphasis or (normal if explicit_normal else DEFAULT_EMPHASIS_FONT_ASSET_ID)
    return normal, emphasis


def distinct_font_assets_share_family(
    normal_asset_id: str | None,
    normal_font: ResolvedFont | None,
    emphasis_asset_id: str | None,
    emphasis_font: ResolvedFont | None,
) -> bool:
    """Whether two distinct files would be ambiguous to libass by family.

    ASS selects a face by family plus style flags, not by our asset id. Exposing
    two distinct assets with the same family in one fontsdir can therefore make
    libass render a different face from the file whose hmtx table the planner
    measured. V2 captions fail closed on this condition.
    """

    return bool(
        normal_asset_id
        and emphasis_asset_id
        and normal_asset_id != emphasis_asset_id
        and normal_font is not None
        and emphasis_font is not None
        and normal_font.family_name.strip().casefold()
        == emphasis_font.family_name.strip().casefold()
    )


def is_font_collection(font: ResolvedFont | None) -> bool:
    """Whether libass may select a different face than planner fontNumber=0."""

    return bool(font is not None and font.source_path.suffix.lower() == ".ttc")


def resolve_font_asset(
    *,
    font_asset_id: str | None,
    runtime_dir: Path,
    source_artifact_for_asset,
    artifact_path,
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
    resolved = resolve_subtitle_font(
        font_path=font_path,
        runtime_dir=runtime_dir,
    )
    return resolved, None if resolved is not None else font_asset_id


def resolve_subtitle_font(
    *,
    font_path: Path,
    runtime_dir: Path,
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
    target = (
        _convert_web_font(source, runtime_dir)
        if source.suffix.lower() in _WEB_FONT_EXTENSIONS
        else _stage_sfnt_font(source, runtime_dir)
    )
    if target is None:
        return None

    family = _read_family_name(target)
    if not family:
        logger.warning("[fonts] selected font %s has no readable family name; ignoring", source)
        return None
    if "," in family or "\n" in family or "\r" in family:
        logger.warning("[fonts] selected font %s has an ASS-unsafe family name; ignoring", source)
        return None
    return ResolvedFont(family_name=family, fonts_dir=runtime_dir, source_path=target)


def _stage_sfnt_font(source: Path, runtime_dir: Path) -> Path | None:
    fingerprint = _font_content_fingerprint(source)
    if fingerprint is None:
        return None
    target = runtime_dir / f"font-{fingerprint}{source.suffix.lower()}"
    try:
        if not target.exists():
            shutil.copy2(source, target)
    except OSError as exc:  # pragma: no cover - filesystem edge
        logger.warning("[fonts] failed to stage font %s -> %s: %s", source, target, exc)
        return None
    if not _fonttools_can_open(target):
        logger.warning("[fonts] selected font %s is corrupt or unreadable; ignoring", source)
        return None
    return target


def _convert_web_font(source: Path, runtime_dir: Path) -> Path | None:
    """Convert WOFF/WOFF2 to a native sfnt file that libass can load reliably."""

    try:
        from fontTools.ttLib import TTFont
    except Exception:
        logger.warning("[fonts] fontTools is unavailable; cannot convert %s", source)
        return None
    font = None
    temp_target: Path | None = None
    try:
        fingerprint = _font_content_fingerprint(source)
        if fingerprint is None:
            return None
        font = TTFont(str(source), fontNumber=0)
        extension = ".otf" if font.sfntVersion == "OTTO" else ".ttf"
        # Content-address the native file: distinct font assets routinely share
        # generic upload names such as ``font.otf`` and must coexist in one flat
        # libass fontsdir without overwriting one another.
        target = runtime_dir / f"font-{fingerprint}{extension}"
        temp_target = target.with_suffix(f"{target.suffix}.tmp")
        font.flavor = None
        font.save(str(temp_target))
        temp_target.replace(target)
        return target if _fonttools_can_open(target) else None
    except Exception as exc:
        logger.warning("[fonts] failed to convert web font %s: %s", source, exc)
        return None
    finally:
        if font is not None:
            font.close()
        if temp_target is not None and temp_target.exists():
            try:
                temp_target.unlink()
            except OSError:  # pragma: no cover - best-effort temporary cleanup
                pass


def _font_content_fingerprint(source: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        logger.warning("[fonts] failed to fingerprint font %s: %s", source, exc)
        return None
    return digest.hexdigest()[:20]


def _fonttools_can_open(path: Path) -> bool:
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(str(path), fontNumber=0, lazy=True)
        font.close()
        return True
    except Exception:
        return False


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
            record = name_table.getDebugName(_NAME_ID_FAMILY)
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
            if name_id != _NAME_ID_FAMILY:
                continue
            start = string_offset + offset
            raw = table[start : start + length]
            decoded = _decode_name_record(platform_id, raw)
            if decoded:
                candidates[name_id] = decoded
        return candidates.get(_NAME_ID_FAMILY)
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
