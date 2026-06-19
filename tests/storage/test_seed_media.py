from __future__ import annotations

import json
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from packages.core.storage.database import AnnotationRow, CaseRow, MediaAssetRow
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.seed_media import seed_media_assets
from packages.core.storage.repository import new_id


sqlite3.register_adapter(dict, json.dumps)
sqlite3.register_adapter(list, json.dumps)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _paths(value) -> list[str]:
    if isinstance(value, list) and value and all(isinstance(item, str) and len(item) == 1 for item in value):
        value = "".join(value)
    for _ in range(3):
        if not isinstance(value, str):
            break
        value = json.loads(value)
    return value if isinstance(value, list) else []


def test_seed_media_upgrades_stale_demo_bgm_annotation(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (CaseRow.__table__, MediaAssetRow.__table__, AnnotationRow.__table__):
        table.create(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        session.add(
            CaseRow(
                id="case_demo",
                name="Demo case",
                owner_user_id="usr_admin",
                status="active",
                description="",
            )
        )
        session.add(
            MediaAssetRow(
                id="asset_bgm_demo",
                case_id="case_demo",
                title="Demo BGM",
                kind="bgm",
                source_artifact_id="art_existing",
                tags=[],
                annotation_status="annotated",
                usable=True,
            )
        )
        session.add(
            AnnotationRow(
                id=new_id("ann"),
                asset_id="asset_bgm_demo",
                etag=new_id("etag"),
                canonical_schema="MediaAnnotation.v1",
                canonical={"labels": [], "kind": "bgm"},
                projection_schema="MediaAnnotationProjection.v1",
                projection={},
                editable_paths=["/labels", "/usable", "/title"],
            )
        )
        session.commit()

        seed_media_assets(session, LocalObjectStore(tmp_path / "objects"))

        row = session.query(AnnotationRow).filter_by(asset_id="asset_bgm_demo").one()
        assert row.canonical_schema == "AnnotationV4.v1"
        assert row.canonical["bgm_segments"]
        assert row.canonical["quality_report"]["bgm"]["segment_count"] == 1
        assert "/canonical/bgm_segments" in _paths(row.editable_paths)
