from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from packages.core.contracts import MediaInfo


@dataclass(frozen=True)
class ImportedMediaArtifactData:
    payload: dict[str, Any]
    media_info: MediaInfo | None


def imported_media_artifact_data(
    row: Mapping[str, Any],
    *,
    case_id: str | None,
    title: str,
    kind: str,
    uri: str,
    sha256: str | None,
    probed: MediaInfo | None,
) -> ImportedMediaArtifactData:
    content_type = (
        optional_str(row.get("mime"))
        or (probed.mime_type if probed is not None else None)
        or mimetypes.guess_type(uri)[0]
        or "application/octet-stream"
    )
    duration_sec = (
        probed.duration_sec
        if probed is not None and probed.duration_sec is not None
        else optional_float(row.get("duration_sec"))
    )
    width = probed.width if probed is not None and probed.width is not None else optional_int(row.get("width"))
    height = probed.height if probed is not None and probed.height is not None else optional_int(row.get("height"))
    media_info = probed or media_info_from_import_metadata(
        uri=uri,
        kind=kind,
        content_type=content_type,
        duration_sec=duration_sec,
        width=width,
        height=height,
    )
    payload = {
        "upload_session_id": None,
        "filename": filename_from_uri(uri, fallback=title),
        "content_type": content_type,
        "size_bytes": optional_int(row.get("size_bytes")) or 0,
        "object_uri": uri,
        "sha256": sha256,
        "metadata": {
            "case_id": case_id,
            "title": title,
            "kind": kind,
            "duration_sec": duration_sec if duration_sec is not None else 0,
            "width": width,
            "height": height,
        },
    }
    return ImportedMediaArtifactData(payload=payload, media_info=media_info)


def media_info_from_import_metadata(
    *,
    uri: str,
    kind: str,
    content_type: str,
    duration_sec: float | None,
    width: int | None,
    height: int | None,
) -> MediaInfo | None:
    media_type = media_type_from_metadata(kind, content_type)
    if media_type is None:
        return None
    suffix = Path(urlsplit(uri).path).suffix.lstrip(".")
    return MediaInfo(
        media_type=media_type,
        codec="unknown",
        format=suffix or content_type.split("/")[-1] or "unknown",
        mime_type=content_type,
        duration_sec=None if media_type == "image" else duration_sec,
        width=width,
        height=height,
    )


def media_type_from_metadata(kind: str, content_type: str) -> str | None:
    if content_type.startswith("video/") or kind in {"portrait", "broll", "video"}:
        return "video"
    if content_type.startswith("audio/") or kind in {"bgm", "voice", "voice_reference"}:
        return "audio"
    if content_type.startswith("image/") or kind in {"image", "cover_template"}:
        return "image"
    return None


def filename_from_uri(uri: str, *, fallback: str) -> str:
    filename = Path(unquote(urlsplit(uri).path)).name
    return filename or fallback or "imported-media"


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
