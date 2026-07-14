"""The issue #206 backfill: historical rows must stop pointing the browser at big objects.

The `--limit` progression test is the important one — the first version of this
script filtered "already done" rows in Python AFTER the SQL LIMIT, and writing a
thumbnail bumps `updated_at`, which was the ORDER BY column. A chunked run
therefore re-selected the rows it had just finished and never advanced, while
cheerfully reporting "0 remaining".
"""

from __future__ import annotations

import cv2
import numpy as np

from packages.core.contracts import ArtifactKind
from packages.core.storage.database import ArtifactRow, CaseRow, FinishedVideoRow, MediaAssetRow
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import new_id
from scripts.backfill_cover_thumbnails import backfill_finished_videos, backfill_media_assets


def _png_bytes(width: int = 1080, height: int = 1920) -> bytes:
    gradient = np.linspace(0, 255, width, dtype=np.uint8)
    image = np.repeat(gradient[None, :], height, axis=0)
    ok, buf = cv2.imencode(".png", np.stack([image, image[:, ::-1], image], axis=-1))
    assert ok
    return buf.tobytes()


def _store_png(store: LocalObjectStore, name: str) -> str:
    ref = store.prepare_upload(name, "covers")
    store.put_bytes(ref, _png_bytes())
    return ref.uri


def _case(session, case_id: str) -> None:
    if session.get(CaseRow, case_id) is None:
        session.add(CaseRow(id=case_id, name="backfill", status="active", key_selling_points=[]))


def test_backfill_gives_a_historical_finished_video_a_webp_and_is_idempotent(
    db_session_factory, tmp_path
):
    store = LocalObjectStore(root=tmp_path)
    cover_uri = _store_png(store, "mid.png")
    fv_id = new_id("fv")
    with db_session_factory() as session:
        _case(session, "case_backfill_fv")
        cover_artifact_id = new_id("art")
        session.add(
            ArtifactRow(
                id=cover_artifact_id,
                kind=ArtifactKind.cover_image.value,
                uri=cover_uri,
                payload_schema="uri-only",
                payload={},
            )
        )
        session.add(
            FinishedVideoRow(
                id=fv_id,
                case_id="case_backfill_fv",
                title="historical",
                video_artifact={"artifact_id": "art_v", "kind": "video.finished", "uri": "s3://b/v.mp4"},
                cover_artifact={
                    "artifact_id": cover_artifact_id,
                    "kind": ArtifactKind.cover_image.value,
                    "uri": cover_uri,
                },
                cover_thumb_artifact=None,
                duration_sec=10,
                qc_status="passed",
            )
        )
        session.commit()

    done, failed = backfill_finished_videos(db_session_factory, store, dry_run=False, limit=0)
    assert (done, failed) == (1, 0)

    with db_session_factory() as session:
        row = session.get(FinishedVideoRow, fv_id)
        thumb = row.cover_thumb_artifact
        assert thumb["kind"] == ArtifactKind.cover_thumbnail.value
        assert thumb["uri"].endswith(".webp")
        # The card now downloads this instead of the multi-megabyte cover.
        from packages.core.storage.object_store import parse_object_uri

        thumb_bytes = store.get_bytes(parse_object_uri(thumb["uri"]))
        assert thumb_bytes[:4] == b"RIFF"
        assert len(thumb_bytes) < len(store.get_bytes(parse_object_uri(cover_uri)))
        # The artifact row backing the ref really exists (no dangling ref).
        assert session.get(ArtifactRow, thumb["artifact_id"]) is not None

    # Re-running must be a no-op, not a second thumbnail.
    done, failed = backfill_finished_videos(db_session_factory, store, dry_run=False, limit=0)
    assert (done, failed) == (0, 0)


def test_a_coverless_finished_video_is_not_scanned(db_session_factory, tmp_path):
    """A fail-open export (Seedance) can land with no cover at all.

    Such a row can never get a thumbnail, so it must be excluded from the scan —
    otherwise it matches forever and, under --limit, wedges the window on work that
    can never complete. Note SQLAlchemy stores Python None in a JSONB column as JSON
    'null', which is NOT SQL NULL, so a naive `cover_artifact IS NOT NULL` matches it.
    """
    store = LocalObjectStore(root=tmp_path)
    with db_session_factory() as session:
        _case(session, "case_backfill_nocover")
        session.add(
            FinishedVideoRow(
                id=new_id("fv"),
                case_id="case_backfill_nocover",
                title="coverless",
                video_artifact={"artifact_id": "art_v", "kind": "video.finished", "uri": "s3://b/v.mp4"},
                cover_artifact=None,
                cover_thumb_artifact=None,
                duration_sec=10,
                qc_status="passed",
            )
        )
        session.commit()

    done, failed = backfill_finished_videos(db_session_factory, store, dry_run=False, limit=1)
    assert (done, failed) == (0, 0)


def test_backfill_repoints_a_library_card_from_a_frame_grab_to_a_webp(db_session_factory, tmp_path):
    store = LocalObjectStore(root=tmp_path)
    png_uri = _store_png(store, "mid.png")
    asset_id = new_id("asset")
    with db_session_factory() as session:
        _case(session, "case_backfill_asset")
        session.add(
            MediaAssetRow(
                id=asset_id,
                case_id="case_backfill_asset",
                title="historical",
                kind="video",
                tags=[],
                annotation_status="pending",
                usable=True,
                thumbnail_uri=png_uri,
            )
        )
        session.commit()

    done, failed = backfill_media_assets(db_session_factory, store, dry_run=False, limit=0)
    assert (done, failed) == (1, 0)

    with db_session_factory() as session:
        asset = session.get(MediaAssetRow, asset_id)
        assert asset.thumbnail_uri.endswith(".webp")
        assert asset.thumbnail_uri != png_uri

    done, _failed = backfill_media_assets(db_session_factory, store, dry_run=False, limit=0)
    assert done == 0


def test_chunked_media_backfill_advances_instead_of_rescanning_finished_rows(
    db_session_factory, tmp_path
):
    """Regression: writing a thumbnail bumps updated_at, the old ORDER BY column.

    With the done-filter applied in Python after the LIMIT, `--limit 1` re-selected
    the row it had just written on every subsequent run and never reached the rest.
    """
    store = LocalObjectStore(root=tmp_path)
    asset_ids = []
    with db_session_factory() as session:
        _case(session, "case_backfill_chunk")
        for index in range(3):
            asset_id = new_id("asset")
            asset_ids.append(asset_id)
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    case_id="case_backfill_chunk",
                    title=f"historical-{index}",
                    kind="video",
                    tags=[],
                    annotation_status="pending",
                    usable=True,
                    thumbnail_uri=_store_png(store, f"mid{index}.png"),
                )
            )
        session.commit()

    # Three chunked runs must convert all three rows — one each, never repeating.
    for _ in range(3):
        done, failed = backfill_media_assets(db_session_factory, store, dry_run=False, limit=1)
        assert (done, failed) == (1, 0)

    with db_session_factory() as session:
        converted = [session.get(MediaAssetRow, aid).thumbnail_uri for aid in asset_ids]
    assert all(uri.endswith(".webp") for uri in converted)
    assert len(set(converted)) == 3

    done, _failed = backfill_media_assets(db_session_factory, store, dry_run=False, limit=1)
    assert done == 0
