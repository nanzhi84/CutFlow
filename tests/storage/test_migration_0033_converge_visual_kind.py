"""Regression for migration 0033 (issue #133).

Migration 0026 converged the *historical* ``portrait`` / ``broll`` visual assets
onto ``video``, but the demo media seed kept emitting those kinds, so any DB
bootstrapped after 0026 (or whose API lifespan re-seeded the demo assets)
re-introduced legacy rows behind 0026's back. 0033 re-runs the same idempotent
flip so those residual rows converge before the ``UploadKind`` /
``MediaAssetRecord.kind`` enum/Literal removal makes reading a ``portrait`` /
``broll`` row a hard validation error.

The test proves the rewrite + provenance tag against real Postgres, that
natively-``video`` and non-visual rows are untouched, that the rewrite is
idempotent (including a row that already carries the ``legacy_kind`` tag), and —
critically — that the separate ``selection_ledger.medium`` (A-roll/B-roll track
role) is NOT touched.
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
    "packages/core/storage/alembic/versions/0033_converge_visual_kind.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0033", MIGRATION_PATH)
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
    assert 'revision = "0033_converge_visual_kind"' in text_src
    assert 'down_revision = "0032_voice_case_bindings"' in text_src
    # alembic version_num column is VARCHAR(32); the id must fit.
    assert len("0033_converge_visual_kind") <= 32


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


def test_upgrade_converges_residual_visual_kinds_and_preserves_provenance(db_session_factory):
    with db_session_factory() as session:
        portrait_id = _add_asset(session, kind="portrait", tags=["seed", "usable"])
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
                medium="broll",
                asset_id=broll_id,
                slot_phase="cover",
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

        # Residual legacy visual kinds converge to ``video`` + carry provenance.
        assert portrait.kind == "video"
        assert "legacy_kind:portrait" in portrait.tags
        assert "seed" in portrait.tags  # pre-existing tags preserved
        assert broll.kind == "video"
        assert "legacy_kind:broll" in broll.tags
        assert "scenery" in broll.tags

        # Native video + non-visual rows are untouched (no spurious legacy tag).
        assert native_video.kind == "video"
        assert not any(tag.startswith("legacy_kind:") for tag in native_video.tags)
        assert bgm.kind == "bgm"
        assert not any(tag.startswith("legacy_kind:") for tag in bgm.tags)

        # selection_ledger.medium is a SEPARATE concept — never rewritten.
        assert ledger.medium == "broll"


def test_upgrade_is_idempotent_including_pretagged_rows(db_session_factory):
    with db_session_factory() as session:
        plain_id = _add_asset(session, kind="portrait", tags=[])
        # A row that somehow still reads ``portrait`` but already carries the
        # provenance tag: the ``case when`` guard must not double-append it.
        pretagged_id = _add_asset(session, kind="portrait", tags=["legacy_kind:portrait"])
        session.commit()

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    # Run twice: the second pass finds no portrait/broll rows and is a no-op.
    _run(engine, module.upgrade)
    _run(engine, module.upgrade)

    with engine.connect() as conn:
        rows = {
            row.id: (row.kind, list(row.tags))
            for row in conn.execute(
                text("select id, kind, tags from media_assets where id = any(:ids)"),
                {"ids": [plain_id, pretagged_id]},
            )
        }
    assert rows[plain_id][0] == "video"
    assert rows[plain_id][1].count("legacy_kind:portrait") == 1
    assert rows[pretagged_id][0] == "video"
    assert rows[pretagged_id][1].count("legacy_kind:portrait") == 1


def test_downgrade_restores_kind_from_legacy_tag(db_session_factory):
    with db_session_factory() as session:
        portrait_id = _add_asset(session, kind="portrait", tags=["seed"])
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
    assert "seed" in portrait.tags
    assert broll.kind == "broll"
    assert "legacy_kind:broll" not in broll.tags
    # Natively-video rows (no legacy tag) stay video across the round trip.
    assert native_video.kind == "video"
