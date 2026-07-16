from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from sqlalchemy import select

from packages.core.storage.database import (
    ArtifactRow,
    create_database_engine,
    create_session_factory,
)
from packages.core.storage.object_store import LocalObjectStore, S3ObjectStore
from packages.core.storage.object_store_env import object_store_from_env
from packages.core.storage.tiered_object_store import TieredObjectStore

GENERATED_PREFIXES = ("generated-video", "generated-audio", "subtitles", "covers")


@dataclass(frozen=True)
class Candidate:
    uri: str
    size_bytes: int
    modified_at: float


def _referenced_uris(session_factory) -> set[str]:
    with session_factory() as session:
        rows = session.execute(select(ArtifactRow.uri, ArtifactRow.oss_uri))
        return {uri for row in rows for uri in row if uri}


def _physical_stores(store) -> list[LocalObjectStore | S3ObjectStore]:
    if isinstance(store, TieredObjectStore):
        return [store.durable, store.ephemeral]
    return [store]


def _local_candidates(store: LocalObjectStore) -> list[Candidate]:
    values: list[Candidate] = []
    for prefix in GENERATED_PREFIXES:
        root = store.root / prefix
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            key = path.relative_to(store.root).as_posix()
            values.append(
                Candidate(
                    uri=f"local://{store.bucket}/{key}",
                    size_bytes=stat.st_size,
                    modified_at=stat.st_mtime,
                )
            )
    return values


def _s3_candidates(store: S3ObjectStore) -> list[Candidate]:
    values: list[Candidate] = []
    paginator = store._client.get_paginator("list_objects_v2")
    for prefix in GENERATED_PREFIXES:
        for page in paginator.paginate(Bucket=store.bucket, Prefix=f"{prefix}/"):
            for item in page.get("Contents", []):
                values.append(
                    Candidate(
                        uri=f"s3://{store.bucket}/{item['Key']}",
                        size_bytes=int(item.get("Size") or 0),
                        modified_at=item["LastModified"].timestamp(),
                    )
                )
    return values


def collect_orphans(store, referenced: set[str], *, max_age_hours: float) -> list[Candidate]:
    cutoff = time.time() - max_age_hours * 3600
    candidates: list[Candidate] = []
    for physical in _physical_stores(store):
        if isinstance(physical, LocalObjectStore):
            candidates.extend(_local_candidates(physical))
        elif isinstance(physical, S3ObjectStore):
            candidates.extend(_s3_candidates(physical))
    return sorted(
        (
            candidate
            for candidate in candidates
            if candidate.modified_at < cutoff and candidate.uri not in referenced
        ),
        key=lambda candidate: candidate.uri,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete aged generated objects that have no Artifact database reference."
    )
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete matched objects. Without this flag the command is a dry run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_age_hours <= 0:
        parser.error("--max-age-hours must be greater than zero")
    engine = create_database_engine()
    print(f"Database target: {engine.url.render_as_string(hide_password=True)}")
    store = object_store_from_env()
    referenced = _referenced_uris(create_session_factory(engine))
    orphans = collect_orphans(store, referenced, max_age_hours=args.max_age_hours)
    mode = "DELETE" if args.apply else "DRY-RUN"
    for candidate in orphans:
        print(f"{mode} {candidate.uri} ({candidate.size_bytes} bytes)")
        if args.apply:
            store.delete(candidate.uri)
    print(
        f"Orphan objects: {len(orphans)}; reclaimable bytes: "
        f"{sum(candidate.size_bytes for candidate in orphans)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
