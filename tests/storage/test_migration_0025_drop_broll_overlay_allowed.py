"""Regression for migration 0025 (issue #100).

The un-consumed ``NarrationUnit.broll_overlay_allowed`` field was dropped from the
contract. ``narration.units`` artifacts persisted the key inside each
``payload->'units'[*]`` element (via ``model_dump`` with defaults), so under
``extra="forbid"`` re-reading a legacy unit through ``NarrationUnit.model_validate``
(the path in ``packages/production/pipeline/nodes/broll_planning.py`` and
``broll_coverage_planning.py``) would raise. Unlike 0023/0024 (which strip a
top-level request key), this field lives inside a JSONB *array element*, so the
migration rebuilds ``payload->'units'`` with the key removed from every element.
This test proves a legacy unit is broken before the upgrade and validates cleanly
afterwards, against real Postgres (the migration is PostgreSQL-only).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import text

from packages.core.contracts.artifacts import NarrationUnit
from packages.core.storage.database import ArtifactRow
from packages.core.storage.repository import new_id

MIGRATION_PATH = Path(
    "packages/core/storage/alembic/versions/0025_drop_broll_overlay_allowed.py"
)

_KEY = "broll_overlay_allowed"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0025", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_chains_to_single_head():
    text_src = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "0025_drop_broll_overlay"' in text_src
    assert 'down_revision = "0024_drop_out_strict_fields"' in text_src
    # alembic version_num column is VARCHAR(32); the id must fit.
    assert len("0025_drop_broll_overlay") <= 32


def _legacy_unit(idx: int) -> dict:
    """A NarrationUnit as persisted before #100: a full, otherwise valid unit dict
    whose serialized form still carries the dropped ``broll_overlay_allowed`` key."""
    unit = NarrationUnit(
        unit_id=f"unit_{idx:03d}",
        text="一句旁白",
        start=float(idx),
        end=float(idx) + 0.5,
        confidence=1.0,
        duration=0.5,
        intent="explain",
        pause_after_ms=0,
        hard_end=True,
        boundary_score=0.78,
        portrait_cut_allowed=True,
        boundary_reason="脚本句尾",
    ).model_dump(mode="json")
    # Legacy rows persisted the now-removed key (model_dump default included).
    unit[_KEY] = bool(idx % 2 == 0)
    return unit


def _legacy_narration_payload() -> dict:
    return {
        "source": "estimated",
        "units": [_legacy_unit(1), _legacy_unit(2)],
        "strict": False,
        "warnings": [],
    }


def test_upgrade_strips_unconsumed_broll_overlay_key(db_session_factory):
    payload = _legacy_narration_payload()

    # Legacy units are unreadable under the slimmed contract until migrated.
    for unit in payload["units"]:
        assert _KEY in unit
        with pytest.raises(ValidationError):
            NarrationUnit.model_validate(unit)

    artifact_id = new_id("artifact")
    with db_session_factory() as session:
        session.add(
            ArtifactRow(
                id=artifact_id,
                run_id=new_id("run"),
                kind="narration.units",
                payload_schema="NarrationUnitsArtifact.v1",
                payload=payload,
            )
        )
        session.commit()

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()

    with engine.connect() as conn:
        stored = conn.execute(
            text("select payload from artifacts where id = :id"), {"id": artifact_id}
        ).scalar_one()

    units = stored["units"]
    assert len(units) == 2
    for unit in units:
        assert _KEY not in unit
        # The migrated unit now validates cleanly against the slimmed contract.
        NarrationUnit.model_validate(unit)
    # Untouched supported fields survive on every unit.
    assert [u["unit_id"] for u in units] == ["unit_001", "unit_002"]
    assert units[0]["boundary_reason"] == "脚本句尾"
    assert units[0]["portrait_cut_allowed"] is True
    # Sibling artifact-level keys are preserved.
    assert stored["source"] == "estimated"
    assert stored["strict"] is False


def test_upgrade_is_idempotent_and_skips_other_kinds(db_session_factory):
    narration_id = new_id("artifact")
    other_id = new_id("artifact")
    other_payload = {"units": [{"broll_overlay_allowed": True, "marker": "keep"}]}

    with db_session_factory() as session:
        session.add(
            ArtifactRow(
                id=narration_id,
                run_id=new_id("run"),
                kind="narration.units",
                payload_schema="NarrationUnitsArtifact.v1",
                payload=_legacy_narration_payload(),
            )
        )
        # A non-narration artifact that happens to carry a similarly named key is
        # never touched (kind guard).
        session.add(
            ArtifactRow(
                id=other_id,
                run_id=new_id("run"),
                kind="material.pack",
                payload_schema="MaterialPackArtifact.v1",
                payload=other_payload,
            )
        )
        session.commit()

    engine = db_session_factory.kw["bind"]
    module = _load_migration()
    # Run twice: the second pass must be a no-op (key already gone).
    for _ in range(2):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.upgrade()

    with engine.connect() as conn:
        narration_payload = conn.execute(
            text("select payload from artifacts where id = :id"), {"id": narration_id}
        ).scalar_one()
        kept_payload = conn.execute(
            text("select payload from artifacts where id = :id"), {"id": other_id}
        ).scalar_one()

    for unit in narration_payload["units"]:
        assert _KEY not in unit
    # The unrelated artifact kind keeps its payload verbatim.
    assert kept_payload == other_payload
