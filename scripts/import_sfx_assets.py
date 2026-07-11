#!/usr/bin/env python3
"""Import the CC0 caption SFX starter pack as content-addressed media assets."""

from __future__ import annotations

import argparse
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from packages.core.contracts import ArtifactKind, utcnow
from packages.core.storage.database import ArtifactRow, MediaAssetRow, create_session_factory
from packages.core.storage.object_store_env import object_store_from_env
from packages.core.storage.repository import new_id
from packages.media.assets import store_file
from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_base_args, probe_media

_INTERFACE_ARCHIVE = (
    "https://kenney.nl/media/pages/assets/interface-sounds/"
    "fa43c1dd4d-1677589452/kenney_interface-sounds.zip"
)
_IMPACT_ARCHIVE = (
    "https://kenney.nl/media/pages/assets/impact-sounds/"
    "87b4ddecda-1677589768/kenney_impact-sounds.zip"
)


@dataclass(frozen=True)
class SfxSpec:
    asset_id: str
    title: str
    sfx_class: str
    archive_url: str
    archive_member: str
    source_page: str


SFX_PACK: tuple[SfxSpec, ...] = (
    SfxSpec(
        asset_id="asset_sfx_click",
        title="Caption Click",
        sfx_class="click",
        archive_url=_INTERFACE_ARCHIVE,
        archive_member="Audio/click_001.ogg",
        source_page="https://kenney.nl/assets/interface-sounds",
    ),
    SfxSpec(
        asset_id="asset_sfx_ding",
        title="Caption Ding",
        sfx_class="ding",
        archive_url=_INTERFACE_ARCHIVE,
        archive_member="Audio/confirmation_001.ogg",
        source_page="https://kenney.nl/assets/interface-sounds",
    ),
    SfxSpec(
        asset_id="asset_sfx_whoosh",
        title="Caption Whoosh",
        sfx_class="whoosh",
        archive_url=_INTERFACE_ARCHIVE,
        archive_member="Audio/scroll_001.ogg",
        source_page="https://kenney.nl/assets/interface-sounds",
    ),
    SfxSpec(
        asset_id="asset_sfx_impact",
        title="Caption Impact",
        sfx_class="impact",
        archive_url=_IMPACT_ARCHIVE,
        archive_member="Audio/impactPunch_heavy_002.ogg",
        source_page="https://kenney.nl/assets/impact-sounds",
    ),
)


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


def _download(url: str, target: Path, *, force: bool) -> None:
    if target.exists() and not force:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "cutflow-sfx-importer/1.0"})
    partial = target.with_suffix(target.suffix + ".part")
    with urlopen(request, timeout=120) as response, partial.open("wb") as handle:
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
    partial.replace(target)


def _normalized_file(spec: SfxSpec, cache_dir: Path, *, force: bool) -> Path:
    archive_name = "impact.zip" if spec.archive_url == _IMPACT_ARCHIVE else "interface.zip"
    archive_path = cache_dir / archive_name
    _download(spec.archive_url, archive_path, force=force)
    raw_path = cache_dir / "raw" / Path(spec.archive_member).name
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if force or not raw_path.exists():
        with zipfile.ZipFile(archive_path) as archive:
            raw_path.write_bytes(archive.read(spec.archive_member))
    output_path = cache_dir / "normalized" / f"{spec.asset_id}.wav"
    if force or not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        FfmpegRunner(timeout_sec=30).run(
            [
                *ffmpeg_base_args(quiet_args=("-y", "-hide_banner", "-loglevel", "error")),
                "-i",
                str(raw_path),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=7",
                "-ar",
                "48000",
                "-ac",
                "2",
                str(output_path),
            ]
        )
    info = probe_media(output_path)
    if not info.duration_sec or info.duration_sec >= 1.5:
        raise RuntimeError(f"{spec.asset_id} must be shorter than 1.5 seconds")
    return output_path


def _payload(spec: SfxSpec, *, object_uri: str, size_bytes: int, sha256: str) -> dict:
    return {
        "upload_session_id": None,
        "filename": f"{spec.asset_id}.wav",
        "content_type": "audio/wav",
        "size_bytes": size_bytes,
        "object_uri": object_uri,
        "sha256": sha256,
        "metadata": {
            "importer": "scripts/import_sfx_assets.py",
            "pack": "caption_liveliness_v3",
            "asset_id": spec.asset_id,
            "sfx_class": spec.sfx_class,
            "license": "CC0-1.0",
            "source_page": spec.source_page,
            "archive_url": spec.archive_url,
            "archive_member": spec.archive_member,
        },
    }


def _upsert(session, spec: SfxSpec, *, object_uri: str, size_bytes: int, sha256: str):
    asset = session.get(MediaAssetRow, spec.asset_id)
    artifact = session.get(ArtifactRow, asset.source_artifact_id) if asset else None
    payload = _payload(spec, object_uri=object_uri, size_bytes=size_bytes, sha256=sha256)
    action = "updated"
    if artifact is None or artifact.sha256 != sha256:
        artifact = ArtifactRow(
            id=new_id("art"),
            case_id=None,
            kind=ArtifactKind.uploaded_file.value,
            uri=object_uri,
            sha256=sha256,
            size_bytes=size_bytes,
            media_info=None,
            payload_schema="UploadedFileArtifact.v1",
            payload=payload,
        )
        session.add(artifact)
        session.flush()
        action = "created"
    else:
        artifact.uri = object_uri
        artifact.size_bytes = size_bytes
        artifact.payload = payload
        artifact.updated_at = utcnow()
    tags = sorted(
        {"sfx", "caption_liveliness_v3", "license:CC0-1.0", f"sfx_class:{spec.sfx_class}"}
    )
    if asset is None:
        asset = MediaAssetRow(
            id=spec.asset_id,
            case_id=None,
            title=spec.title,
            kind="sfx",
            source_artifact_id=artifact.id,
            tags=tags,
            annotation_status="annotated",
            usable=True,
        )
        session.add(asset)
    else:
        asset.title = spec.title
        asset.kind = "sfx"
        asset.source_artifact_id = artifact.id
        asset.tags = tags
        asset.annotation_status = "annotated"
        asset.usable = True
        asset.updated_at = utcnow()
    return action, asset.id, artifact.id


def import_sfx(*, cache_dir: Path, force_download: bool) -> list[dict[str, str]]:
    store = object_store_from_env()
    session_factory = create_session_factory()
    results = []
    with session_factory() as session:
        for spec in SFX_PACK:
            path = _normalized_file(spec, cache_dir, force=force_download)
            stored = store_file(
                store,
                path,
                purpose="sfx",
                addressed=True,
                content_type="audio/wav",
            )
            action, asset_id, artifact_id = _upsert(
                session,
                spec,
                object_uri=stored.ref.uri,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            )
            results.append({"action": action, "asset_id": asset_id, "artifact_id": artifact_id})
        session.commit()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.local"))
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".data/sfx-imports/caption-liveliness-v3"),
    )
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()
    _load_env_file(args.env_file)
    for result in import_sfx(cache_dir=args.cache_dir, force_download=args.force_download):
        print(f"{result['action']:7s} {result['asset_id']} -> {result['artifact_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
