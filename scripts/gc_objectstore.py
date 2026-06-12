from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path


GENERATED_PREFIXES = ("generated-video", "generated-audio", "subtitles", "covers")


def _default_root() -> Path:
    return Path(os.getenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", ".data/objectstore"))


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _old_object_dirs(root: Path, *, cutoff: float) -> list[Path]:
    old_dirs: list[Path] = []
    for prefix in GENERATED_PREFIXES:
        prefix_dir = root / prefix
        if not prefix_dir.exists():
            continue
        for object_dir in prefix_dir.iterdir():
            if object_dir.is_dir() and object_dir.stat().st_mtime < cutoff:
                old_dirs.append(object_dir)
    return old_dirs


def collect_old_objects(root: Path, *, max_age_hours: float) -> list[tuple[Path, int]]:
    cutoff = time.time() - max_age_hours * 3600
    return [(path, _dir_size(path)) for path in _old_object_dirs(root, cutoff=cutoff)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Garbage collect old generated ObjectStore objects.")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="ObjectStore root. Defaults to CUTAGENT_LOCAL_OBJECTSTORE_PATH or .data/objectstore.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24,
        help="Delete generated object directories older than this many hours.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete matched objects. Without this flag the command only prints a dry-run plan.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root or _default_root()
    old_objects = collect_old_objects(root, max_age_hours=args.max_age_hours)
    total_bytes = sum(size for _, size in old_objects)
    mode = "DELETE" if args.apply else "DRY-RUN"

    for path, size in old_objects:
        print(f"{mode} {path} ({size} bytes)")
        if args.apply:
            shutil.rmtree(path)
    print(f"Total reclaimable bytes: {total_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
