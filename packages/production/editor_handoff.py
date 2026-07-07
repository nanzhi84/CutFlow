from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from packages.core.storage.object_store import ObjectStore
from . import jianying_draft_json as package_json


@dataclass(frozen=True)
class EditorHandoffAsset:
    role: str
    artifact_id: str
    kind: str
    source_path: Path


@dataclass(frozen=True)
class EditorHandoffInput:
    finished_video_id: str
    package_format: str = "zip"
    assets: list[EditorHandoffAsset] = field(default_factory=list)
    effects: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EditorHandoffBuild:
    package_uri: str
    sha256: str
    size_bytes: int
    manifest: dict


class EditorHandoffBuilder:
    def __init__(self, object_store: ObjectStore) -> None:
        self.object_store = object_store

    def build(self, source: EditorHandoffInput) -> EditorHandoffBuild:
        with tempfile.TemporaryDirectory(prefix="cutagent-handoff-") as directory:
            root = Path(directory) / "handoff"
            root.mkdir(parents=True, exist_ok=True)
            assets_manifest: dict[str, list[dict]] = {}
            for asset in source.assets:
                relative = _copy_asset(root, asset)
                assets_manifest.setdefault(asset.role, []).append(
                    {
                        "artifact_id": asset.artifact_id,
                        "kind": asset.kind,
                        "path": relative,
                    }
                )
            manifest = {
                "finished_video_id": source.finished_video_id,
                "format": source.package_format,
                "assets": assets_manifest,
                "artifact_ids": [asset.artifact_id for asset in source.assets],
                "effects": source.effects,
            }
            if source.effects:
                package_json.dump_json(root / "effects.json", source.effects)
            package_json.dump_json(root / "manifest.json", manifest)
            zip_path = Path(directory) / f"{source.finished_video_id}-editor-handoff.zip"
            package_json.zip_root(root, zip_path)
            stored = self.object_store.put_bytes(
                self.object_store.prepare_upload(zip_path.name, "editor-handoffs"),
                zip_path.read_bytes(),
            )
            manifest = {**manifest, "package_uri": stored.ref.uri, "size_bytes": stored.size_bytes, "sha256": stored.sha256}
            return EditorHandoffBuild(stored.ref.uri, stored.sha256, stored.size_bytes, manifest)


def _copy_asset(root: Path, asset: EditorHandoffAsset) -> str:
    target_dir = root / "assets" / asset.role
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / asset.source_path.name
    index = 1
    while target.exists():
        target = target_dir / f"{asset.source_path.stem}_{index}{asset.source_path.suffix}"
        index += 1
    shutil.copy2(asset.source_path, target)
    return target.relative_to(root).as_posix()
