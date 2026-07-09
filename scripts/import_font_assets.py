#!/usr/bin/env python3
"""Download the starter font pack and index it as global font media assets."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from packages.core.contracts import ArtifactKind, utcnow
from packages.core.storage.database import ArtifactRow, MediaAssetRow, create_session_factory
from packages.core.storage.object_store_env import object_store_from_env
from packages.core.storage.repository import new_id
from packages.media.assets import store_file


@dataclass(frozen=True)
class FontSpec:
    asset_id: str
    title: str
    filename: str
    url: str
    content_type: str
    family: str
    weight: int
    style: str
    usages: tuple[str, ...]
    source_repo: str
    license_url: str


FONT_PACK: tuple[FontSpec, ...] = (
    FontSpec(
        asset_id="asset_font_noto_sans_cjk_sc_regular",
        title="Noto Sans CJK SC Regular",
        filename="NotoSansCJKsc-Regular.otf",
        url="https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/"
        "SimplifiedChinese/NotoSansCJKsc-Regular.otf",
        content_type="font/otf",
        family="Noto Sans CJK SC",
        weight=400,
        style="sans",
        usages=("normal_subtitle",),
        source_repo="https://github.com/notofonts/noto-cjk",
        license_url="https://github.com/notofonts/noto-cjk/blob/main/LICENSE",
    ),
    FontSpec(
        asset_id="asset_font_noto_sans_cjk_sc_bold",
        title="Noto Sans CJK SC Bold",
        filename="NotoSansCJKsc-Bold.otf",
        url="https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/"
        "SimplifiedChinese/NotoSansCJKsc-Bold.otf",
        content_type="font/otf",
        family="Noto Sans CJK SC",
        weight=700,
        style="sans",
        usages=("normal_subtitle", "huazi"),
        source_repo="https://github.com/notofonts/noto-cjk",
        license_url="https://github.com/notofonts/noto-cjk/blob/main/LICENSE",
    ),
    FontSpec(
        asset_id="asset_font_noto_serif_cjk_sc_regular",
        title="Noto Serif CJK SC Regular",
        filename="NotoSerifCJKsc-Regular.otf",
        url="https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/OTF/"
        "SimplifiedChinese/NotoSerifCJKsc-Regular.otf",
        content_type="font/otf",
        family="Noto Serif CJK SC",
        weight=400,
        style="serif",
        usages=("normal_subtitle",),
        source_repo="https://github.com/notofonts/noto-cjk",
        license_url="https://github.com/notofonts/noto-cjk/blob/main/LICENSE",
    ),
    FontSpec(
        asset_id="asset_font_noto_serif_cjk_sc_bold",
        title="Noto Serif CJK SC Bold",
        filename="NotoSerifCJKsc-Bold.otf",
        url="https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/OTF/"
        "SimplifiedChinese/NotoSerifCJKsc-Bold.otf",
        content_type="font/otf",
        family="Noto Serif CJK SC",
        weight=700,
        style="serif",
        usages=("huazi",),
        source_repo="https://github.com/notofonts/noto-cjk",
        license_url="https://github.com/notofonts/noto-cjk/blob/main/LICENSE",
    ),
    FontSpec(
        asset_id="asset_font_lxgw_wenkai_regular",
        title="LXGW WenKai Regular",
        filename="LXGWWenKai-Regular.ttf",
        url="https://raw.githubusercontent.com/lxgw/LxgwWenKai/main/fonts/TTF/"
        "LXGWWenKai-Regular.ttf",
        content_type="font/ttf",
        family="LXGW WenKai",
        weight=400,
        style="handwritten",
        usages=("huazi",),
        source_repo="https://github.com/lxgw/LxgwWenKai",
        license_url="https://github.com/lxgw/LxgwWenKai/blob/main/LICENSE",
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
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            os.environ[key] = value


def _download(url: str, target: Path, *, force: bool) -> None:
    if target.exists() and not force:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    request = Request(url, headers={"User-Agent": "cutflow-font-importer/1.0"})
    with urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
    tmp.replace(target)


def _tags(spec: FontSpec) -> list[str]:
    tags = [
        "font",
        "starter_pack",
        "license:OFL-1.1",
        "lang:zh-CN",
        f"family:{spec.family}",
        f"style:{spec.style}",
        f"weight:{spec.weight}",
    ]
    tags.extend(f"usage:{usage}" for usage in spec.usages)
    return sorted(dict.fromkeys(tags))


def _payload(spec: FontSpec, *, object_uri: str, size_bytes: int, sha256: str) -> dict:
    return {
        "upload_session_id": None,
        "filename": spec.filename,
        "content_type": spec.content_type,
        "size_bytes": size_bytes,
        "object_uri": object_uri,
        "sha256": sha256,
        "metadata": {
            "importer": "scripts/import_font_assets.py",
            "pack": "starter",
            "asset_id": spec.asset_id,
            "family": spec.family,
            "weight": spec.weight,
            "style": spec.style,
            "usages": list(spec.usages),
            "license": "OFL-1.1",
            "source_url": spec.url,
            "source_repo": spec.source_repo,
            "license_url": spec.license_url,
        },
    }


def _upsert_font_asset(session, spec: FontSpec, *, object_uri: str, size_bytes: int, sha256: str):
    asset = session.get(MediaAssetRow, spec.asset_id)
    existing_artifact = session.get(ArtifactRow, asset.source_artifact_id) if asset else None
    if existing_artifact is not None and existing_artifact.sha256 == sha256:
        artifact = existing_artifact
        artifact.uri = object_uri
        artifact.size_bytes = size_bytes
        artifact.payload_schema = "UploadedFileArtifact.v1"
        artifact.payload = _payload(spec, object_uri=object_uri, size_bytes=size_bytes, sha256=sha256)
        artifact.updated_at = utcnow()
        action = "updated"
    else:
        artifact = ArtifactRow(
            id=new_id("art"),
            case_id=None,
            kind=ArtifactKind.uploaded_file.value,
            uri=object_uri,
            sha256=sha256,
            size_bytes=size_bytes,
            media_info=None,
            payload_schema="UploadedFileArtifact.v1",
            payload=_payload(spec, object_uri=object_uri, size_bytes=size_bytes, sha256=sha256),
        )
        session.add(artifact)
        session.flush()
        action = "created"

    if asset is None:
        asset = MediaAssetRow(
            id=spec.asset_id,
            case_id=None,
            title=spec.title,
            kind="font",
            source_artifact_id=artifact.id,
            tags=_tags(spec),
            annotation_status="annotated",
            usable=True,
        )
        session.add(asset)
    else:
        asset.case_id = None
        asset.title = spec.title
        asset.kind = "font"
        asset.source_artifact_id = artifact.id
        asset.tags = _tags(spec)
        asset.annotation_status = "annotated"
        asset.usable = True
        asset.updated_at = utcnow()
    return action, asset.id, artifact.id


def import_fonts(*, download_dir: Path, force_download: bool) -> list[dict[str, str]]:
    store = object_store_from_env()
    session_factory = create_session_factory()
    results: list[dict[str, str]] = []
    with session_factory() as session:
        for spec in FONT_PACK:
            local_path = download_dir / spec.filename
            _download(spec.url, local_path, force=force_download)
            stored = store_file(
                store,
                local_path,
                purpose="font",
                addressed=True,
                content_type=spec.content_type,
            )
            action, asset_id, artifact_id = _upsert_font_asset(
                session,
                spec,
                object_uri=stored.ref.uri,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            )
            results.append(
                {
                    "action": action,
                    "asset_id": asset_id,
                    "artifact_id": artifact_id,
                    "uri": stored.ref.uri,
                    "title": spec.title,
                }
            )
        session.commit()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(os.environ.get("CUTAGENT_ENV_FILE", ".env.local")),
        help="Environment file to load before building Cutagent settings.",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=Path(".data/font-imports/starter-pack"),
        help="Local cache directory for downloaded font files.",
    )
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()

    _load_env_file(args.env_file)
    results = import_fonts(download_dir=args.download_dir, force_download=args.force_download)
    for result in results:
        print(
            f"{result['action']:7s} {result['asset_id']} -> {result['artifact_id']} "
            f"{result['uri']} ({result['title']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
