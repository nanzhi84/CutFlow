"""Query-count regression for ``hydrate_workflow_runtime_snapshot`` (issue #68).

The worker hydrates a run snapshot before every node activity. Annotation and
source-artifact loading used to issue one query *per media asset*, an N+1 over
the whole shared media pool (this case + the global ``case_id IS NULL`` pool).
This gated DB test pins the batched shape: the number of ``annotations`` SELECTs
during hydrate must stay at 1 regardless of how many assets exist.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

if os.getenv("CUTAGENT_RUN_DB_TESTS") != "1":
    pytest.skip("Set CUTAGENT_RUN_DB_TESTS=1 to run database integration tests.", allow_module_level=True)

from apps.api.main import app
from packages.core.storage import Repository
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import AnnotationRow, MediaAssetRow
from packages.core.storage.repository import new_id


def _session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def _seed_assets_with_annotations(session_factory, *, case_id: str, count: int) -> list[str]:
    asset_ids = [new_id("asset") for _ in range(count)]
    # Commit assets first so the annotations' asset_id FK is satisfiable.
    with session_factory() as session:
        for index, asset_id in enumerate(asset_ids):
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    case_id=case_id,
                    title=f"hydrate-n1-{index}",
                    kind="broll",
                    annotation_status="annotated",
                )
            )
        session.commit()
    with session_factory() as session:
        for asset_id in asset_ids:
            session.add(
                AnnotationRow(
                    id=new_id("ann"),
                    asset_id=asset_id,
                    etag=f"etag-{asset_id}",
                    canonical_schema="AnnotationV4.v1",
                    canonical={},
                    projection_schema="v1",
                    projection={},
                )
            )
        session.commit()
    return asset_ids


def _count_annotation_selects(engine, work):
    seen = {"count": 0}

    def _before_cursor_execute(_conn, _cursor, statement, *_args, **_kwargs):
        # Normalise whitespace: SQLAlchemy renders ``FROM`` on its own line, so a
        # naive `" from annotations"` substring (leading space) would miss it.
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select") and "from annotations" in normalized:
            seen["count"] += 1

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        work()
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)
    return seen["count"]


def test_hydrate_annotation_query_count_stays_one_regardless_of_asset_count():
    session_factory = _session_factory()
    with TestClient(app) as client:
        prod_repo = app.state.sqlalchemy_production_repository
        assert prod_repo is not None, "needs the SQLAlchemy production repository"

        login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert login.status_code == 200, login.text
        created = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "Hydrate N+1 regression",
                "script": "用一个脚本验证 hydrate 不产生 per-asset 查询。",
                "voice": {"voice_id": "voice_sandbox"},
                "strictness": {"strict_timestamps": False},
            },
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["initial_run"]["id"]

        # Listen on the SAME engine the production repository hydrates through.
        with prod_repo.session_factory() as session:
            engine = session.get_bind()

        # Seed a handful of assets, hydrate, then seed many more and hydrate
        # again. A per-asset (N+1) annotation query would make the second count
        # jump; the batched IN query keeps both at exactly 1.
        few_ids = _seed_assets_with_annotations(session_factory, case_id="case_demo", count=3)
        few = _count_annotation_selects(
            engine, lambda: prod_repo.hydrate_workflow_runtime_snapshot(Repository(), run_id)
        )

        many_ids = _seed_assets_with_annotations(session_factory, case_id="case_demo", count=25)
        repository = Repository()
        many = _count_annotation_selects(
            engine, lambda: prod_repo.hydrate_workflow_runtime_snapshot(repository, run_id)
        )

    # Assert outside the lifespan context so a failure does not race the
    # TestClient/lifespan teardown.
    assert few == 1, f"expected 1 batched annotation query with few assets, got {few}"
    assert many == 1, f"annotation query count scaled with asset count: {many}"
    # Every seeded annotation is still hydrated into the runtime repository.
    for asset_id in few_ids + many_ids:
        assert asset_id in repository.annotations
