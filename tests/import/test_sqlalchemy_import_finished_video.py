"""SQLAlchemy finished-video import: video_number uniqueness is handled gracefully.

The migration 0016 added uq_finished_videos_case_video_number (case_id, video_number).
Imports take video_number straight from the external manifest, so a duplicate would
otherwise raise IntegrityError at the single whole-batch commit -> 500 AND roll back
every row. These tests pin the graceful behavior: the colliding row fails on its own,
valid rows still persist, and NULL numbers stay unconstrained.
"""

import json
import sqlite3

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from packages.core.contracts import CreateImportBatchRequest
from packages.core.storage.database import (
    ArtifactRow,
    CaseRow,
    FinishedVideoRow,
    ImportBatchReportRow,
)
from packages.production import SqlAlchemyProductionRepository


sqlite3.register_adapter(list, json.dumps)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _repository_with_sqlite():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (
        CaseRow.__table__,
        ArtifactRow.__table__,
        FinishedVideoRow.__table__,
        ImportBatchReportRow.__table__,
    ):
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
        session.commit()
    return SqlAlchemyProductionRepository(session_factory), session_factory


def _finished_row(external_id: str, *, video_number: str | None = None, case_id: str = "case_demo") -> dict:
    row = {
        "external_id": external_id,
        "case_id": case_id,
        "title": f"Imported {external_id}",
        "uri": f"s3://cutagent-durable/imports/{external_id}.mp4",
        "duration_sec": 8.0,
    }
    if video_number is not None:
        row["video_number"] = video_number
    return row


def test_import_finished_video_with_explicit_number_persists():
    repository, session_factory = _repository_with_sqlite()

    report = repository.create_import_batch(
        CreateImportBatchRequest(
            import_type="finished_video",
            rows=[_finished_row("fv1", video_number="V-003")],
        ),
        request_id="req_fv_basic",
    )

    assert report is not None
    assert report.created_count == 1
    assert report.failed_count == 0
    with session_factory() as session:
        finished = session.get(FinishedVideoRow, report.results[0].internal_id)
        assert finished is not None
        assert finished.video_number == "V-003"


def test_import_duplicate_number_in_same_batch_fails_only_that_row():
    repository, session_factory = _repository_with_sqlite()

    report = repository.create_import_batch(
        CreateImportBatchRequest(
            import_type="finished_video",
            rows=[
                _finished_row("fv1", video_number="V-001"),
                _finished_row("fv2", video_number="V-001"),  # collides with fv1
                _finished_row("fv3", video_number="V-002"),  # distinct -> ok
            ],
        ),
        request_id="req_fv_dupe_batch",
    )

    assert report is not None
    # The batch is NOT aborted: 2 valid rows persist, only the duplicate fails.
    assert report.created_count == 2
    assert report.failed_count == 1
    statuses = {result.row_index: result.status for result in report.results}
    assert statuses == {0: "created", 1: "failed", 2: "created"}
    failed = next(r for r in report.results if r.status == "failed")
    assert failed.error is not None
    assert "V-001" in failed.error.message
    with session_factory() as session:
        numbers = sorted(
            n
            for (n,) in session.execute(
                select(FinishedVideoRow.video_number).where(FinishedVideoRow.case_id == "case_demo")
            )
        )
    assert numbers == ["V-001", "V-002"]  # exactly one V-001 landed


def test_import_number_colliding_with_existing_db_row_fails_gracefully():
    repository, session_factory = _repository_with_sqlite()

    first = repository.create_import_batch(
        CreateImportBatchRequest(
            import_type="finished_video",
            rows=[_finished_row("fv1", video_number="V-005")],
        ),
        request_id="req_fv_seed",
    )
    assert first is not None and first.created_count == 1

    # A later, separate batch reuses V-005 -> graceful row failure, NOT a 500 / IntegrityError.
    second = repository.create_import_batch(
        CreateImportBatchRequest(
            import_type="finished_video",
            rows=[_finished_row("fv2", video_number="V-005")],
        ),
        request_id="req_fv_collide",
    )

    assert second is not None
    assert second.created_count == 0
    assert second.failed_count == 1
    assert second.results[0].status == "failed"
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(FinishedVideoRow).where(FinishedVideoRow.video_number == "V-005")
            )
        )
    assert len(rows) == 1  # original untouched, duplicate never inserted


def test_import_null_video_numbers_are_unconstrained():
    repository, session_factory = _repository_with_sqlite()

    report = repository.create_import_batch(
        CreateImportBatchRequest(
            import_type="finished_video",
            rows=[_finished_row("fv1"), _finished_row("fv2"), _finished_row("fv3")],
        ),
        request_id="req_fv_nulls",
    )

    assert report is not None
    assert report.created_count == 3
    assert report.failed_count == 0
    with session_factory() as session:
        rows = list(session.scalars(select(FinishedVideoRow)))
    assert len(rows) == 3
    assert all(row.video_number is None for row in rows)
