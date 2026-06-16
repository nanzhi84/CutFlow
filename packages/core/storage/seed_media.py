"""Seed real source artifacts for the demo media assets into SQL.

The in-memory backend backfills these on construction via
``LocalRuntimeAdapter._ensure_seed_media_assets``. The SQL / Temporal backend,
however, rehydrates media assets from SQL per activity and never runs that
in-memory bootstrap, so the demo assets ship with ``source_artifact_id = None``
and no backing ``ArtifactRow``. The digital-human workflow then fails at
``MaterialPackPlanning`` with ``artifact.missing`` ("Media source artifact is
missing.") the moment it resolves a portrait/broll source.

``seed_media_assets`` closes that gap for SQL deployments (CI, docker-compose):
it generates the same probe-able demo media, stores it in the configured object
store, and persists an ``uploaded_file`` ``ArtifactRow`` (carrying ``media_info``,
which is the only hard dependency of MaterialPackPlanning's scoring) plus the
``source_artifact_id`` back-reference on the asset. Keep the specs in sync with
``LocalRuntimeAdapter._ensure_seed_media_assets``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from packages.core.contracts import ArtifactKind
from packages.core.storage.database import AnnotationRow, ArtifactRow, MediaAssetRow
from packages.core.storage.object_store import ObjectStore
from packages.core.storage.repository import demo_portrait_annotation_v4, new_id
from packages.media.assets import store_file
from packages.media.rendering import generate_seed_audio, generate_seed_video
from packages.media.video.ffmpeg import probe_media

_SEED_MEDIA_SPECS: dict[str, dict] = {
    "asset_portrait_demo": {
        "filename": "portrait_demo_15s.mp4",
        "content_type": "video/mp4",
        "generator": lambda path: generate_seed_video(path, duration_sec=15, width=320, height=568, fps=30),
    },
    "asset_broll_demo": {
        "filename": "broll_demo_4s.mp4",
        "content_type": "video/mp4",
        "generator": lambda path: generate_seed_video(path, duration_sec=4, width=320, height=568, fps=30),
    },
    "asset_bgm_demo": {
        "filename": "bgm_demo_15s.wav",
        "content_type": "audio/wav",
        "generator": lambda path: generate_seed_audio(path, duration_sec=15),
    },
}


def seed_media_assets(session: Session, object_store: ObjectStore) -> int:
    """Persist source artifacts for the demo media assets; return how many seeded.

    Idempotent: assets that already carry a ``source_artifact_id`` (or that are
    absent entirely) are skipped, so re-running bootstrap is a no-op.
    """
    seed_dir = Path(".data/generated-media/seed")
    seed_dir.mkdir(parents=True, exist_ok=True)
    seeded = 0
    for asset_id, spec in _SEED_MEDIA_SPECS.items():
        asset_row = session.get(MediaAssetRow, asset_id)
        if asset_row is None or asset_row.source_artifact_id:
            continue
        path = seed_dir / str(spec["filename"])
        if not path.exists():
            spec["generator"](path)
        media_info = probe_media(path)
        stored = store_file(object_store, path, purpose="seed-media", addressed=True)
        artifact_id = new_id("art")
        session.add(
            ArtifactRow(
                id=artifact_id,
                case_id=asset_row.case_id,
                kind=ArtifactKind.uploaded_file.value,
                uri=stored.ref.uri,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                media_info=media_info.model_dump(mode="json"),
                payload_schema="UploadedFileArtifact.v1",
                payload={
                    "upload_session_id": None,
                    "filename": path.name,
                    "content_type": spec["content_type"],
                    "size_bytes": stored.size_bytes,
                    "object_uri": stored.ref.uri,
                    "sha256": stored.sha256,
                    "metadata": {"seed": "true", "asset_id": asset_id},
                },
            )
        )
        asset_row.source_artifact_id = artifact_id
        asset_row.annotation_status = "annotated"
        asset_row.usable = True
        seeded += 1

    # Back the annotated demo portrait with a real V4 annotation so clip-level
    # material selection yields an A-roll candidate (the production pipeline requires
    # an annotation — no whole-asset fallback). Idempotent: skipped if one exists.
    portrait_row = session.get(MediaAssetRow, "asset_portrait_demo")
    if portrait_row is not None and (
        session.query(AnnotationRow).filter_by(asset_id="asset_portrait_demo").first() is None
    ):
        session.add(
            AnnotationRow(
                id=new_id("ann"),
                asset_id="asset_portrait_demo",
                etag=new_id("etag"),
                canonical_schema="AnnotationV4.v1",
                canonical=demo_portrait_annotation_v4(portrait_row.case_id).model_dump(mode="json"),
                projection_schema="MediaAnnotationProjection.v1",
                projection={},
                editable_paths=["/labels", "/usable", "/title"],
            )
        )
    session.commit()
    return seeded
