"""Backfill small WebP thumbnails for objects that predate issue #206.

Before #206, the Outputs card rendered the full-size cover (a lossless
1080x1920 PNG, ~2.3 MB) and the library card rendered a full-resolution frame
grab — or, for image assets, the original upload. New exports/uploads now emit a
~30 KB WebP alongside, but historical rows still point the browser at the big
object. This walks those rows once and gives them a thumbnail.

Idempotent: rows that already have a thumbnail are skipped, so it is safe to
re-run (and safe to interrupt — each row commits on its own).

    python scripts/backfill_cover_thumbnails.py --dry-run
    python scripts/backfill_cover_thumbnails.py
    python scripts/backfill_cover_thumbnails.py --limit 50
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import and_, or_, select  # noqa: E402

from packages.core.contracts import ArtifactKind  # noqa: E402
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory  # noqa: E402
from packages.core.storage.database import (  # noqa: E402
    ArtifactRow,
    FinishedVideoRow,
    MediaAssetRow,
)
from packages.core.storage.object_store import ObjectStore, get_object_store  # noqa: E402
from packages.core.storage.repository import new_id  # noqa: E402
from packages.media.assets import store_file  # noqa: E402
from packages.media.cover_image import (  # noqa: E402
    THUMBNAIL_CONTENT_TYPE,
    THUMBNAIL_SUFFIX,
    build_cover_thumbnail_bytes,
)

_SIGNABLE = ("s3://", "local://")


def _thumbnail_from(store: ObjectStore, uri: str) -> tuple[str, str] | None:
    """Encode + store a WebP thumbnail of ``uri``. Returns (uri, sha256), or None
    when the source is unreadable or not a decodable image."""
    if not uri.startswith(_SIGNABLE):
        return None
    try:
        from packages.media.assets import local_object_path

        content = Path(local_object_path(store, uri)).read_bytes()
        thumbnail = build_cover_thumbnail_bytes(content)
    except Exception as exc:  # noqa: BLE001 — one bad object must not stop the sweep
        print(f"    skip (unreadable/undecodable): {uri} — {exc}")
        return None
    with tempfile.TemporaryDirectory(prefix="cutagent-backfill-thumb-") as directory:
        path = Path(directory) / f"cover_thumb{THUMBNAIL_SUFFIX}"
        path.write_bytes(thumbnail)
        stored = store_file(store, path, purpose="covers", content_type=THUMBNAIL_CONTENT_TYPE)
    return stored.ref.uri, stored.sha256


def backfill_finished_videos(session_factory, store: ObjectStore, *, dry_run: bool, limit: int):
    done = failed = 0
    with session_factory() as session:
        # Scan exactly the RESOLVABLE rows. `cover_artifact.isnot(None)` is not enough:
        # a coverless export (Seedance's fail-open frame cover) stores JSON 'null',
        # which is not SQL NULL, so such a row would match forever and — under
        # --limit — wedge the window on a row that can never be filled.
        statement = (
            select(FinishedVideoRow)
            .where(FinishedVideoRow.cover_thumb_artifact.is_(None))
            .where(FinishedVideoRow.cover_artifact["uri"].astext.isnot(None))
            .order_by(FinishedVideoRow.created_at.desc())
        )
        if limit:
            statement = statement.limit(limit)
        rows = list(session.scalars(statement))

    print(f"finished_videos without a cover thumbnail: {len(rows)}")
    for row in rows:
        cover = row.cover_artifact if isinstance(row.cover_artifact, dict) else {}
        uri = cover.get("uri")
        if not isinstance(uri, str):
            continue
        print(f"  {row.id} <- {uri}")
        if dry_run:
            continue
        result = _thumbnail_from(store, uri)
        if result is None:
            failed += 1
            continue
        thumb_uri, sha256 = result
        with session_factory() as session:
            artifact = ArtifactRow(
                id=new_id("art"),
                kind=ArtifactKind.cover_thumbnail.value,
                uri=thumb_uri,
                sha256=sha256,
                payload_schema="uri-only",
                payload={"source_artifact_id": cover.get("artifact_id")},
            )
            session.add(artifact)
            session.flush()
            target = session.get(FinishedVideoRow, row.id)
            # Re-check under the write: a concurrent export may have filled it in.
            if target is not None and target.cover_thumb_artifact is None:
                target.cover_thumb_artifact = {
                    "artifact_id": artifact.id,
                    "kind": ArtifactKind.cover_thumbnail.value,
                    "uri": thumb_uri,
                    "schema_version": "v1",
                    "sha256": sha256,
                }
                done += 1
            session.commit()
    return done, failed


def backfill_media_assets(session_factory, store: ObjectStore, *, dry_run: bool, limit: int):
    """Give library cards a small thumbnail.

    Two populations: assets with NO thumbnail_uri (image uploads, whose card signs
    the original), and assets whose thumbnail_uri is a full-resolution frame grab.
    Both are repointed at a WebP; the original objects are left in place (the frame
    grabs are still cover source material).
    """
    done = failed = 0
    with session_factory() as session:
        # The "already done" test MUST be a SQL predicate, not a Python filter after
        # the LIMIT: writing a thumbnail bumps updated_at (TimestampMixin.onupdate), so
        # a --limit run ordered by updated_at would re-select the rows it just finished
        # and never advance. Order by the immutable id for a stable, progressing scan.
        statement = (
            select(MediaAssetRow)
            .where(MediaAssetRow.kind.in_(("image", "video")))
            .where(
                or_(
                    # A non-WebP thumbnail (a full-res frame grab) we can shrink...
                    and_(
                        MediaAssetRow.thumbnail_uri.isnot(None),
                        ~MediaAssetRow.thumbnail_uri.like(f"%{THUMBNAIL_SUFFIX}"),
                    ),
                    # ...or an image with no thumbnail at all, whose card signs the
                    # ORIGINAL upload today. A VIDEO with no thumbnail has nothing to
                    # shrink (deriving one would need ffmpeg), so it is excluded here
                    # rather than left to match the predicate forever and wedge --limit.
                    and_(
                        MediaAssetRow.thumbnail_uri.is_(None),
                        MediaAssetRow.kind == "image",
                        MediaAssetRow.source_artifact_id.isnot(None),
                    ),
                )
            )
            .order_by(MediaAssetRow.id)
        )
        if limit:
            statement = statement.limit(limit)
        pending = [
            (row.id, row.thumbnail_uri, row.kind, row.source_artifact_id)
            for row in session.scalars(statement)
        ]
        sources: dict[str, str] = {}
        for asset_id, thumbnail_uri, kind, source_artifact_id in pending:
            source = thumbnail_uri
            if not source and kind == "image" and source_artifact_id:
                artifact = session.get(ArtifactRow, source_artifact_id)
                source = artifact.uri if artifact is not None else None
            if isinstance(source, str) and source.startswith(_SIGNABLE):
                sources[asset_id] = source

    print(f"media_assets without a WebP thumbnail: {len(sources)}")
    for asset_id, source in sources.items():
        print(f"  {asset_id} <- {source}")
        if dry_run:
            continue
        result = _thumbnail_from(store, source)
        if result is None:
            failed += 1
            continue
        thumb_uri, _sha256 = result
        with session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is not None:
                asset.thumbnail_uri = thumb_uri
                done += 1
            session.commit()
    return done, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="List what would change, write nothing.")
    parser.add_argument("--limit", type=int, default=0, help="Cap rows scanned per table (0 = all). A row whose source object fails to encode stays in the window, so prefer the default for a full backfill.")
    parser.add_argument("--skip-finished-videos", action="store_true")
    parser.add_argument("--skip-media-assets", action="store_true")
    args = parser.parse_args()

    session_factory = get_sqlalchemy_session_factory()
    store = get_object_store()

    total_done = total_failed = 0
    if not args.skip_finished_videos:
        done, failed = backfill_finished_videos(
            session_factory, store, dry_run=args.dry_run, limit=args.limit
        )
        total_done += done
        total_failed += failed
    if not args.skip_media_assets:
        done, failed = backfill_media_assets(
            session_factory, store, dry_run=args.dry_run, limit=args.limit
        )
        total_done += done
        total_failed += failed

    if args.dry_run:
        print("\ndry run — nothing written")
    else:
        print(f"\nthumbnails written: {total_done}; sources skipped: {total_failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
