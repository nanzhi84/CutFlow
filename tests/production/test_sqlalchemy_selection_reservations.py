from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from packages.core.contracts import (
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    JobStatus,
    JobType,
    RunStatus,
    WorkflowRun,
    utcnow,
)
from packages.core.storage.database import (
    CaseRow,
    JobRow,
    NodeRunRow,
    SelectionLedgerRow,
    SelectionReservationRow,
    VoiceProfileRow,
    WorkflowRunRow,
)
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production import SqlAlchemyProductionRepository


class StaticHydrateSession:
    def __init__(
        self,
        rows_by_model: dict[type, list[object]],
        rows_by_key: dict[tuple[type, str], object],
    ) -> None:
        self.rows_by_model = rows_by_model
        self.rows_by_key = rows_by_key

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, model, key):
        return self.rows_by_key.get((model, key))

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        return self.rows_by_model.get(entity, [])


class RecordingSyncSession:
    def __init__(self) -> None:
        self.merged: list[object] = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def merge(self, row):
        self.merged.append(row)
        return row

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def get(self, model, key):
        return None


class ReservationConflictSyncSession(RecordingSyncSession):
    def commit(self) -> None:
        class Diag:
            constraint_name = "uq_selection_reservations_active_slot"

        class Original(Exception):
            diag = Diag()

            def __str__(self) -> str:
                return "duplicate key value violates unique constraint uq_selection_reservations_active_slot"

        raise IntegrityError("insert", {}, Original())


def _timestamped(row):
    now = utcnow()
    if hasattr(row, "schema_version"):
        row.schema_version = "v1"
    row.created_at = now
    if hasattr(row, "updated_at"):
        row.updated_at = now
    return row


def _job_row() -> JobRow:
    return _timestamped(
        JobRow(
            id="job_reservation",
            type=JobType.digital_human_video.value,
            status=JobStatus.queued.value,
            case_id="case_demo",
            created_by="usr_admin",
            request_schema="DigitalHumanVideoRequest.v1",
            request={
                "case_id": "case_demo",
                "script": "并发预占需要进入 worker。",
                "voice": {"voice_id": "voice_demo_cn"},
                "strictness": {"strict_timestamps": False},
            },
        )
    )


def _run_row(job_id: str) -> WorkflowRunRow:
    return _timestamped(
        WorkflowRunRow(
            id="run_reservation",
            job_id=job_id,
            case_id="case_demo",
            workflow_template_id="digital_human_v2",
            workflow_version="v1",
            status=RunStatus.admitted.value,
            requested_by="usr_admin",
            run_attempt=1,
        )
    )


def _case_row() -> CaseRow:
    return _timestamped(
        CaseRow(
            id="case_demo",
            name="Demo Case",
            owner_user_id="usr_admin",
            status="active",
            description=None,
            industry=None,
            product=None,
            target_audience=None,
        )
    )


def test_hydrate_workflow_runtime_snapshot_loads_active_selection_reservations():
    job = _job_row()
    run = _run_row(job.id)
    reservation = SelectionReservationRow(
        id="resv_parallel_bgm",
        case_id="case_demo",
        run_id="run_parallel",
        medium="bgm",
        asset_id="asset_bgm_song",
        diversity_key=None,
        status="reserved",
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(minutes=30),
        committed_at=None,
        released_at=None,
    )
    rows_by_model = {
        NodeRunRow: [],
        VoiceProfileRow: [],
        SelectionLedgerRow: [],
        SelectionReservationRow: [reservation],
    }
    rows_by_key = {
        (JobRow, job.id): job,
        (WorkflowRunRow, run.id): run,
        (CaseRow, "case_demo"): _case_row(),
    }
    production_repository = SqlAlchemyProductionRepository(
        lambda: StaticHydrateSession(rows_by_model, rows_by_key)
    )
    runtime_repository = Repository()

    production_repository.hydrate_workflow_runtime_snapshot(runtime_repository, run.id)

    active = runtime_repository.active_selection_reservations(
        case_id="case_demo",
        medium="bgm",
        exclude_run_id=run.id,
    )
    assert [(item.run_id, item.asset_id, item.status) for item in active] == [
        ("run_parallel", "asset_bgm_song", "reserved")
    ]


def test_sync_workflow_snapshot_persists_run_selection_reservations():
    session = RecordingSyncSession()
    production_repository = SqlAlchemyProductionRepository(lambda: session)
    repository = Repository()
    job = Job(
        id="job_reservation",
        type=JobType.digital_human_video,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="sync reservations",
            voice={"voice_id": "voice_demo_cn"},
            strictness={"strict_timestamps": False},
        ),
    )
    run = WorkflowRun(
        id="run_reservation",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    repository.reserve_selections(
        case_id="case_demo",
        run_id=run.id,
        medium="portrait",
        asset_ids=["asset_portrait_demo"],
    )

    production_repository.sync_workflow_snapshot(job=job, run=run, repository=repository)

    rows = [row for row in session.merged if isinstance(row, SelectionReservationRow)]
    assert [(row.run_id, row.medium, row.asset_id, row.status) for row in rows] == [
        ("run_reservation", "portrait", "asset_portrait_demo", "reserved")
    ]
    assert session.committed is True


def test_sync_workflow_snapshot_turns_active_slot_conflict_into_retryable_node_error():
    session = ReservationConflictSyncSession()
    production_repository = SqlAlchemyProductionRepository(lambda: session)
    repository = Repository()
    job = Job(
        id="job_reservation",
        type=JobType.digital_human_video,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="sync reservations",
            voice={"voice_id": "voice_demo_cn"},
            strictness={"strict_timestamps": False},
        ),
    )
    run = WorkflowRun(
        id="run_reservation",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    repository.reserve_selections(
        case_id="case_demo",
        run_id=run.id,
        medium="bgm",
        asset_ids=["asset_bgm_demo"],
    )

    with pytest.raises(NodeExecutionError) as exc:
        production_repository.sync_workflow_snapshot(job=job, run=run, repository=repository)

    assert exc.value.error.code == ErrorCode.validation_conflict
    assert exc.value.error.retryable is True
    assert exc.value.error.details["constraint"] == "uq_selection_reservations_active_slot"
