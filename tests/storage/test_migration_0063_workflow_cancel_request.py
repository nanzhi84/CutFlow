from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

REVISION = "0063_workflow_cancel_request"


def test_migration_revision_is_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    migration = script.get_revision(REVISION)

    assert script.get_heads() == ["0064_caption_style_intensity"]
    assert migration is not None
    assert migration.down_revision == "0062_drop_v1_prompts"
    assert len(REVISION) <= 32


def test_migration_adds_cancellation_fence_columns_and_constraint(
    db_session_factory,
) -> None:
    inspector = inspect(db_session_factory.kw["bind"])

    columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
    constraints = {
        constraint["name"] for constraint in inspector.get_check_constraints("workflow_runs")
    }

    assert {"cancel_mode", "cancel_requested_at"} <= columns
    assert "ck_workflow_runs_workflow_runs_cancel_mode" in constraints
