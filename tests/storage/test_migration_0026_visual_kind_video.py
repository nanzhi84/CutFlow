"""Regression for migration 0026 (issue #99).

Historical visual assets uploaded as ``kind=portrait`` / ``kind=broll`` are
converged onto the unified ``video`` bucket, with the original kind preserved as
a ``legacy_kind:<x>`` tag. New uploads already normalize at the API layer
(``apps/api/services/uploads.py``); this migration rewrites the *historical*
rows. The test proves the rewrite + provenance tag against real Postgres (the
migration is PostgreSQL-only), that natively-``video`` and non-visual rows are
untouched, that the rewrite is idempotent, and — critically — that the separate
``selection_ledger.medium`` (A-roll/B-roll track role) is NOT touched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from packages.core.storage.database import MediaAssetRow, SelectionLedgerRow
from packages.core.storage.repository import new_id

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0026_visual_kind_video.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0026", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn) -> None:
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()


def test_migration_revision_chains_to_single_head():
    text_src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0026_visual_kind_video"' in text_src
    assert 'down_revision = "0025_drop_broll_overlay"' in text_src
    # alembic version_num column is VARCHAR(32); the id must fit.
    assert len("0026_visual_kind_video") <= 32


def _add_asset(session, *, kind: str, tags: list[str]) -> str:
    asset_id = new_id("asset")
    session.add(
        MediaAssetRow(
            id=asset_id,
            case_id=None,
            title=f"{kind} clip",
            kind=kind,
            tags=list(tags),
            annotation_status="pending",
            usable=True,
        )
    )
    return asset_id


def test_upgrade_converges_legacy_visual_kinds_and_preserves_provenance(db_session_factory):
    with db_session_factory() as session:
        portrait_id = _add_asset(session, kind="portrait", tags=["office"])
        broll_id = _add_asset(session, kind="broll", tags=["scenery"])
        video_id = _add_asset(session, kind="video", tags=["mixed"])
        bgm_id = _add_asset(session, kind="bgm", tags=["calm"])
        # A selection-ledger row whose medium MUST survive untouched (it is a
        # track-role concept, not an asset kind).
        ledger_id = new_id("sel")
        session.add(
            SelectionLedgerRow(
                id=ledger_id,
                case_id="case_x",
                run_id="run_x",
                medium="portrait",
                asset_id=portrait_id,
                slot_phase="main",
            )
        )
        session.commit()

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    _run(engine, module.upgrade)

    with db_session_factory() as session:
        portrait = session.get(MediaAssetRow, portrait_id)
        broll = session.get(MediaAssetRow, broll_id)
        native_video = session.get(MediaAssetRow, video_id)
        bgm = session.get(MediaAssetRow, bgm_id)
        ledger = session.get(SelectionLedgerRow, ledger_id)

        # Legacy visual kinds converge to ``video`` + carry their provenance tag.
        assert portrait.kind == "video"
        assert "legacy_kind:portrait" in portrait.tags
        assert "office" in portrait.tags  # pre-existing tags preserved
        assert broll.kind == "video"
        assert "legacy_kind:broll" in broll.tags
        assert "scenery" in broll.tags

        # Native video + non-visual rows are untouched (no spurious legacy tag).
        assert native_video.kind == "video"
        assert not any(tag.startswith("legacy_kind:") for tag in native_video.tags)
        assert bgm.kind == "bgm"
        assert not any(tag.startswith("legacy_kind:") for tag in bgm.tags)

        # selection_ledger.medium is a SEPARATE concept — never rewritten.
        assert ledger.medium == "portrait"


def test_upgrade_is_idempotent(db_session_factory):
    with db_session_factory() as session:
        portrait_id = _add_asset(session, kind="portrait", tags=[])
        session.commit()

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    # Run twice: the second pass finds no portrait/broll rows and is a no-op
    # (no duplicate ``legacy_kind`` tag).
    _run(engine, module.upgrade)
    _run(engine, module.upgrade)

    with engine.connect() as conn:
        row = conn.execute(
            text("select kind, tags from media_assets where id = :id"),
            {"id": portrait_id},
        ).one()
    assert row.kind == "video"
    assert list(row.tags).count("legacy_kind:portrait") == 1


def test_downgrade_restores_kind_from_legacy_tag(db_session_factory):
    with db_session_factory() as session:
        portrait_id = _add_asset(session, kind="portrait", tags=["office"])
        broll_id = _add_asset(session, kind="broll", tags=[])
        native_video_id = _add_asset(session, kind="video", tags=["mixed"])
        session.commit()

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    _run(engine, module.upgrade)
    _run(engine, module.downgrade)

    with db_session_factory() as session:
        portrait = session.get(MediaAssetRow, portrait_id)
        broll = session.get(MediaAssetRow, broll_id)
        native_video = session.get(MediaAssetRow, native_video_id)

    # Best-effort restore: kind reverted, provenance tag removed.
    assert portrait.kind == "portrait"
    assert "legacy_kind:portrait" not in portrait.tags
    assert "office" in portrait.tags
    assert broll.kind == "broll"
    assert "legacy_kind:broll" not in broll.tags
    # Natively-video rows (no legacy tag) stay video across the round trip.
    assert native_video.kind == "video"
