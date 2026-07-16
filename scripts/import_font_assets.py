#!/usr/bin/env python3
"""Download the starter font pack and index it as global font media assets."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
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
    url: str | None
    sha256: str
    content_type: str
    family: str
    weight: int
    style: str
    usages: tuple[str, ...]
    source_repo: str
    license_url: str
    license: str
    archive_url: str | None = None
    archive_member: str | None = None
    archive_sha256: str | None = None


FONT_PACK: tuple[FontSpec, ...] = (
    FontSpec(
        asset_id="asset_font_noto_sans_cjk_sc_regular",
        title="Noto Sans CJK SC Regular",
        filename="NotoSansCJKsc-Regular.otf",
        url="https://raw.githubusercontent.com/notofonts/noto-cjk/"
        "f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/OTF/"
        "SimplifiedChinese/NotoSansCJKsc-Regular.otf",
        sha256="2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
        content_type="font/otf",
        family="Noto Sans CJK SC",
        weight=400,
        style="sans",
        usages=("normal_subtitle",),
        source_repo="https://github.com/notofonts/noto-cjk",
        license_url="https://github.com/notofonts/noto-cjk/blob/main/LICENSE",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_noto_sans_cjk_sc_bold",
        title="Noto Sans CJK SC Bold",
        filename="NotoSansCJKsc-Bold.otf",
        url="https://raw.githubusercontent.com/notofonts/noto-cjk/"
        "f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/OTF/"
        "SimplifiedChinese/NotoSansCJKsc-Bold.otf",
        sha256="b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a",
        content_type="font/otf",
        family="Noto Sans CJK SC",
        weight=700,
        style="sans",
        usages=("normal_subtitle", "huazi"),
        source_repo="https://github.com/notofonts/noto-cjk",
        license_url="https://github.com/notofonts/noto-cjk/blob/main/LICENSE",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_noto_serif_cjk_sc_regular",
        title="Noto Serif CJK SC Regular",
        filename="NotoSerifCJKsc-Regular.otf",
        url="https://raw.githubusercontent.com/notofonts/noto-cjk/"
        "f8d157532fbfaeda587e826d4cd5b21a49186f7c/Serif/OTF/"
        "SimplifiedChinese/NotoSerifCJKsc-Regular.otf",
        sha256="2a2eae2628df83556c54018c41e20fa532c1b862c5256ae8b3f23feb918d12ca",
        content_type="font/otf",
        family="Noto Serif CJK SC",
        weight=400,
        style="serif",
        usages=("normal_subtitle",),
        source_repo="https://github.com/notofonts/noto-cjk",
        license_url="https://github.com/notofonts/noto-cjk/blob/main/LICENSE",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_noto_serif_cjk_sc_bold",
        title="Noto Serif CJK SC Bold",
        filename="NotoSerifCJKsc-Bold.otf",
        url="https://raw.githubusercontent.com/notofonts/noto-cjk/"
        "f8d157532fbfaeda587e826d4cd5b21a49186f7c/Serif/OTF/"
        "SimplifiedChinese/NotoSerifCJKsc-Bold.otf",
        sha256="8af07d4b6c2e82bcc72a30e066eaf295f11b9424f4aad2eaa9fe0e9c3b38fc73",
        content_type="font/otf",
        family="Noto Serif CJK SC",
        weight=700,
        style="serif",
        usages=("huazi",),
        source_repo="https://github.com/notofonts/noto-cjk",
        license_url="https://github.com/notofonts/noto-cjk/blob/main/LICENSE",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_lxgw_wenkai_regular",
        title="LXGW WenKai Regular",
        filename="LXGWWenKai-Regular.ttf",
        url="https://raw.githubusercontent.com/lxgw/LxgwWenKai/"
        "ce97ea8371eb6557f881d6dddd94ae7ccdc33d7e/fonts/TTF/"
        "LXGWWenKai-Regular.ttf",
        sha256="39ad71264b588165b469e35e6afb162a378dacd1f95348160240ba9038ac3009",
        content_type="font/ttf",
        family="LXGW WenKai",
        weight=400,
        style="handwritten",
        usages=("huazi",),
        source_repo="https://github.com/lxgw/LxgwWenKai",
        license_url="https://github.com/lxgw/LxgwWenKai/blob/main/LICENSE",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_zcool_kuaile",
        title="ZCOOL KuaiLe Regular",
        filename="ZCOOLKuaiLe-Regular.ttf",
        url="https://raw.githubusercontent.com/google/fonts/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/"
        "zcoolkuaile/ZCOOLKuaiLe-Regular.ttf",
        sha256="812a6fc1fe54b6d73a419245c32dfeba8aa33104d5be90d1cf6af082007cb71d",
        content_type="font/ttf",
        family="ZCOOL KuaiLe",
        weight=400,
        style="pop_round_handwritten",
        usages=("huazi",),
        source_repo="https://github.com/google/fonts",
        license_url="https://github.com/google/fonts/blob/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/zcoolkuaile/OFL.txt",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_zcool_qingke_huangyou",
        title="ZCOOL QingKe HuangYou Regular",
        filename="ZCOOLQingKeHuangYou-Regular.ttf",
        url="https://raw.githubusercontent.com/google/fonts/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/"
        "zcoolqingkehuangyou/ZCOOLQingKeHuangYou-Regular.ttf",
        sha256="54f0c0df4308cd74cd0f2fd3494ae054dbc4a1fd6fa7d71f4807eb4cdd8b4136",
        content_type="font/ttf",
        family="ZCOOL QingKe HuangYou",
        weight=400,
        style="pop_rounded",
        usages=("huazi",),
        source_repo="https://github.com/google/fonts",
        license_url="https://github.com/google/fonts/blob/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/"
        "zcoolqingkehuangyou/OFL.txt",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_mashanzheng",
        title="Ma Shan Zheng Regular",
        filename="MaShanZheng-Regular.ttf",
        url="https://raw.githubusercontent.com/google/fonts/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/"
        "mashanzheng/MaShanZheng-Regular.ttf",
        sha256="b844c59bf20bf530e41c20d6ff12b383b23a2e553b9b68cc89f070869213155d",
        content_type="font/ttf",
        family="Ma Shan Zheng",
        weight=400,
        style="brush_handwritten",
        usages=("huazi",),
        source_repo="https://github.com/google/fonts",
        license_url="https://github.com/google/fonts/blob/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/mashanzheng/OFL.txt",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_zhimangxing",
        title="Zhi Mang Xing Regular",
        filename="ZhiMangXing-Regular.ttf",
        url="https://raw.githubusercontent.com/google/fonts/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/"
        "zhimangxing/ZhiMangXing-Regular.ttf",
        sha256="644e0cae9b40f0b10ab729a01bd32032e3973bac22be3dccae01bf6ae7fde969",
        content_type="font/ttf",
        family="Zhi Mang Xing",
        weight=400,
        style="running_handwritten",
        usages=("huazi",),
        source_repo="https://github.com/google/fonts",
        license_url="https://github.com/google/fonts/blob/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/zhimangxing/OFL.txt",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_longcang",
        title="Long Cang Regular",
        filename="LongCang-Regular.ttf",
        url="https://raw.githubusercontent.com/google/fonts/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/"
        "longcang/LongCang-Regular.ttf",
        sha256="e5bf2c3f24ef2327c6f136d8f73e2f9dfdf44896fdbeb35a9515f44777bb91bc",
        content_type="font/ttf",
        family="Long Cang",
        weight=400,
        style="pen_handwritten",
        usages=("huazi",),
        source_repo="https://github.com/google/fonts",
        license_url="https://github.com/google/fonts/blob/"
        "26c5c976d82d50c24a8f0a7ac455e0a7c639c226/ofl/longcang/OFL.txt",
        license="OFL-1.1",
    ),
    FontSpec(
        asset_id="asset_font_smiley_sans",
        title="Smiley Sans Oblique",
        filename="SmileySans-Oblique.ttf",
        url=None,
        sha256="b447d7e781f08bc95c4c9f23ba71ed2b8ebb639aa7184485c71c4ca5afcd25c4",
        content_type="font/ttf",
        family="Smiley Sans Oblique",
        weight=400,
        style="modern_oblique",
        usages=("huazi",),
        source_repo="https://github.com/atelier-anchor/smiley-sans",
        license_url="https://github.com/atelier-anchor/smiley-sans/blob/v2.0.1/LICENSE",
        license="OFL-1.1",
        archive_url="https://github.com/atelier-anchor/smiley-sans/releases/download/"
        "v2.0.1/smiley-sans-v2.0.1.zip",
        archive_member="SmileySans-Oblique.ttf",
        archive_sha256="299c0be6c960ae37361762eca76f7d0cd516615435bb96c0d4b98a1e70178a07",
    ),
    FontSpec(
        asset_id="asset_font_lxgw_marker",
        title="LXGW Marker Gothic Regular",
        filename="LXGWMarkerGothic-Regular.ttf",
        url="https://raw.githubusercontent.com/lxgw/LxgwMarkerGothic/"
        "8c45e4f1a347afc84d1db5b94449a9523da07331/fonts/ttf/"
        "LXGWMarkerGothic-Regular.ttf",
        sha256="9a1e46379442856b9fc64de1d9bd4120903780990e28d99fd6415cadee78a47d",
        content_type="font/ttf",
        family="LXGW Marker Gothic",
        weight=400,
        style="marker_rounded",
        usages=("huazi",),
        source_repo="https://github.com/lxgw/LxgwMarkerGothic",
        license_url="https://github.com/lxgw/LxgwMarkerGothic/blob/"
        "8c45e4f1a347afc84d1db5b94449a9523da07331/OFL.txt",
        license="OFL-1.1",
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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path, *, expected_sha256: str, force: bool) -> None:
    if target.exists() and not force and _file_sha256(target) == expected_sha256:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    request = Request(url, headers={"User-Agent": "cutflow-font-importer/1.0"})
    with urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
    actual_sha256 = _file_sha256(tmp)
    if actual_sha256 != expected_sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"font integrity check failed for {target.name}: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
    tmp.replace(target)


def _materialize_font(spec: FontSpec, download_dir: Path, *, force: bool) -> Path:
    target = download_dir / spec.filename
    if target.exists() and not force and _file_sha256(target) == spec.sha256:
        return target
    if spec.archive_url is None:
        if not spec.url:
            raise RuntimeError(f"{spec.asset_id} has no direct font URL")
        _download(spec.url, target, expected_sha256=spec.sha256, force=force)
        return target
    if not spec.archive_member or not spec.archive_sha256:
        raise RuntimeError(f"{spec.asset_id} archive source is incomplete")

    archive_name = Path(urlparse(spec.archive_url).path).name or f"{spec.asset_id}.zip"
    archive_path = download_dir / "archives" / archive_name
    _download(
        spec.archive_url,
        archive_path,
        expected_sha256=spec.archive_sha256,
        force=force,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with zipfile.ZipFile(archive_path) as archive, archive.open(spec.archive_member) as source:
            with partial.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        actual_sha256 = _file_sha256(partial)
        if actual_sha256 != spec.sha256:
            raise RuntimeError(
                f"font integrity check failed for {target.name}: "
                f"expected {spec.sha256}, got {actual_sha256}"
            )
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return target


def _tags(spec: FontSpec) -> list[str]:
    tags = [
        "font",
        "starter_pack",
        f"license:{spec.license}",
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
            "license": spec.license,
            "source_url": spec.archive_url or spec.url,
            "source_repo": spec.source_repo,
            "license_url": spec.license_url,
            "archive_member": spec.archive_member,
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
            local_path = _materialize_font(spec, download_dir, force=force_download)
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
