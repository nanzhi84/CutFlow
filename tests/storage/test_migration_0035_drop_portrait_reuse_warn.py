"""Regression for migration 0035.

PR #153 removes the ``portrait.asset_reuse_relaxed`` warning from the public
contract. Rows written while PR #152 was active may still carry that code in
``node_runs.warnings`` or ``node_runs.degradations``; once the enum is gone, those
rows fail to hydrate through ``node_run_row_to_contract``. The migration strips
only that legacy code and leaves supported warning/degradation entries intact.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError

from packages.core.contracts import WarningCode
from packages.core.storage.database import JobRow, NodeRunRow, WorkflowRunRow
from packages.core.storage.repository import new_id
from packages.production.sqlalchemy_mappers import node_run_row_to_contract

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0035_drop_portrait_reuse_warn.py"
)

_LEGACY_CODE = "portrait.asset_reuse_relaxed"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0035", MIGRATION_PATH)
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
    assert 'revision = "0035_drop_portrait_reuse_warn"' in text_src
    assert 'down_revision = "0034_dashscope_unbounded_qwen37"' in text_src
    assert len("0035_drop_portrait_reuse_warn") <= 32


def _seed_node_run(session, *, warnings: list[str], degradations: list[dict]) -> str:
    job_id = new_id("job")
    run_id = new_id("run")
    node_run_id = new_id("node")
    session.add(
        JobRow(
            id=job_id,
            type="digital_human_video",
            status="succeeded",
            request_schema="DigitalHumanVideoRequest.v1",
            request={},
        )
    )
    session.add(
        WorkflowRunRow(
            id=run_id,
            job_id=job_id,
            workflow_template_id="digital_human_editing_agent_v2",
            workflow_version="v1",
            status="succeeded",
        )
    )
    session.flush()
    session.add(
        NodeRunRow(
            id=node_run_id,
            run_id=run_id,
            node_id="MediaSelectionAgentPlanning",
            node_version="v1",
            status="degraded",
            input_manifest_hash="sha256:test",
            warnings=warnings,
            degradations=degradations,
        )
    )
    return node_run_id


def test_upgrade_removes_legacy_portrait_reuse_code_from_node_runs(db_session_factory):
    valid_warning = WarningCode.media_selection_agent_deterministic_fallback.value
    valid_degradation = {
        "code": WarningCode.broll_insertions_dropped_geometry.value,
        "message": "B-roll dropped.",
        "node_id": "MediaSelectionAgentPlanning",
        "affects_true_yield": False,
    }
    legacy_degradation = {
        "code": _LEGACY_CODE,
        "message": "Relaxed portrait reuse.",
        "node_id": "MediaSelectionAgentPlanning",
        "affects_true_yield": False,
    }

    with db_session_factory() as session:
        node_run_id = _seed_node_run(
            session,
            warnings=[valid_warning, _LEGACY_CODE, _LEGACY_CODE],
            degradations=[legacy_degradation, valid_degradation],
        )
        session.commit()

        with pytest.raises(ValidationError):
            node_run_row_to_contract(session.get(NodeRunRow, node_run_id))

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    _run(engine, module.upgrade)
    _run(engine, module.upgrade)

    with db_session_factory() as session:
        row = session.get(NodeRunRow, node_run_id)
        assert row.warnings == [valid_warning]
        assert row.degradations == [valid_degradation]

        node_run = node_run_row_to_contract(row)
        assert node_run.warnings == [WarningCode.media_selection_agent_deterministic_fallback]
        assert [notice.code for notice in node_run.degradations] == [
            WarningCode.broll_insertions_dropped_geometry
        ]


def test_upgrade_can_leave_empty_warning_and_degradation_lists(db_session_factory):
    with db_session_factory() as session:
        node_run_id = _seed_node_run(
            session,
            warnings=[_LEGACY_CODE],
            degradations=[
                {
                    "code": _LEGACY_CODE,
                    "message": "Relaxed portrait reuse.",
                }
            ],
        )
        session.commit()

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    _run(engine, module.upgrade)

    with db_session_factory() as session:
        row = session.get(NodeRunRow, node_run_id)
        assert row.warnings == []
        assert row.degradations == []
        node_run = node_run_row_to_contract(row)
        assert node_run.warnings == []
        assert node_run.degradations == []
