from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.contracts import (
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    JobStatus,
    JobType,
    RunStatus,
    SelectionReservationRecord,
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

    def __exit__(self, _exc_type, _exc, _tb) -> None:
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

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def merge(self, row):
        self.merged.append(row)
        return row

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def get(self, model, key, **_kwargs):
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


def test_hydrate_loads_all_current_run_selection_reservations(db_session_factory):
    job = _job_row()
    run = _run_row(job.id)
    now = utcnow()
    current_run_reservations = [
        SelectionReservationRow(
            id=f"resv_current_{index:03d}",
            case_id="case_demo",
            run_id=run.id,
            medium="portrait",
            asset_id=f"asset_current_{index:03d}",
            diversity_key=None,
            status="reserved",
            created_at=now - timedelta(minutes=10),
            expires_at=now + timedelta(minutes=30),
            committed_at=None,
            released_at=None,
        )
        for index in range(110)
    ]
    newer_parallel_reservations = [
        SelectionReservationRow(
            id=f"resv_parallel_{index:03d}",
            case_id="case_demo",
            run_id="run_parallel",
            medium="portrait",
            asset_id=f"asset_parallel_{index:03d}",
            diversity_key=None,
            status="reserved",
            created_at=now + timedelta(seconds=index),
            expires_at=now + timedelta(minutes=30),
            committed_at=None,
            released_at=None,
        )
        for index in range(120)
    ]
    with db_session_factory() as session:
        session.add(job)
        session.add(run)
        session.add_all(current_run_reservations)
        session.add_all(newer_parallel_reservations)
        session.commit()
    production_repository = SqlAlchemyProductionRepository(db_session_factory)
    runtime_repository = Repository()

    production_repository.hydrate_workflow_runtime_snapshot(runtime_repository, run.id)

    owned = [
        item for item in runtime_repository.selection_reservations.values() if item.run_id == run.id
    ]
    assert len(owned) == 110
    assert {item.asset_id for item in owned} == {
        f"asset_current_{index:03d}" for index in range(110)
    }
    parallel = [
        item
        for item in runtime_repository.selection_reservations.values()
        if item.run_id == "run_parallel"
    ]
    assert len(parallel) == 120


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


def test_atomic_conflict_rollback_preserves_a_concurrent_other_run(monkeypatch) -> None:
    session = ReservationConflictSyncSession()
    production_repository = SqlAlchemyProductionRepository(lambda: session)
    repository = Repository()
    concurrent = SelectionReservationRecord(
        case_id="case_other",
        run_id="run_concurrent",
        medium="portrait",
        asset_id="asset_concurrent",
    )
    raise_conflict = session.commit

    def concurrent_commit_then_conflict() -> None:
        repository.selection_reservations[concurrent.id] = concurrent
        raise_conflict()

    monkeypatch.setattr(session, "commit", concurrent_commit_then_conflict)
    monkeypatch.setattr(
        production_repository,
        "_hydrate_selection_reservations",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(NodeExecutionError):
        production_repository.reserve_selection_candidates(
            repository,
            case_id="case_demo",
            run_id="run_loser",
            asset_ids_by_medium={"portrait": ["asset_raced"]},
            diversity_keys_by_medium={"portrait": {}},
        )

    assert repository.selection_reservations == {concurrent.id: concurrent}


def test_atomic_candidate_reservation_refreshes_the_race_winner_before_retry(
    db_session_factory,
):
    production_repository = SqlAlchemyProductionRepository(db_session_factory)
    first = Repository()
    stale_second = Repository()
    batch = {"portrait": ["asset_atomic_race"]}
    diversity = {"portrait": {"asset_atomic_race": "scene:atomic"}}

    first_owned = production_repository.reserve_selection_candidates(
        first,
        case_id="case_demo",
        run_id="run_atomic_first",
        asset_ids_by_medium=batch,
        diversity_keys_by_medium=diversity,
    )

    assert [item.asset_id for item in first_owned["portrait"]] == ["asset_atomic_race"]
    with pytest.raises(NodeExecutionError) as exc:
        production_repository.reserve_selection_candidates(
            stale_second,
            case_id="case_demo",
            run_id="run_atomic_second",
            asset_ids_by_medium=batch,
            diversity_keys_by_medium=diversity,
        )

    assert exc.value.error.code == ErrorCode.validation_conflict
    assert exc.value.error.retryable is True
    refreshed = stale_second.active_selection_reservations(
        case_id="case_demo",
        medium="portrait",
        exclude_run_id="run_atomic_second",
    )
    assert [(item.run_id, item.asset_id) for item in refreshed] == [
        ("run_atomic_first", "asset_atomic_race")
    ]


def test_atomic_candidate_reservation_renews_same_run_expired_row(
    db_session_factory,
):
    production_repository = SqlAlchemyProductionRepository(db_session_factory)
    repository = Repository()
    batch = {"portrait": ["asset_atomic_renew"]}
    diversity = {"portrait": {"asset_atomic_renew": "scene:renew"}}
    first = production_repository.reserve_selection_candidates(
        repository,
        case_id="case_demo",
        run_id="run_atomic_renew",
        asset_ids_by_medium=batch,
        diversity_keys_by_medium=diversity,
    )["portrait"][0]
    expired_at = utcnow() - timedelta(seconds=1)
    with db_session_factory() as session:
        row = session.get(SelectionReservationRow, first.id)
        row.expires_at = expired_at
        session.commit()
    repository.selection_reservations[first.id] = first.model_copy(
        update={"expires_at": expired_at}
    )
    assert repository.active_selection_reservations(
        case_id="case_demo", medium="portrait"
    ) == []

    renewed = production_repository.reserve_selection_candidates(
        repository,
        case_id="case_demo",
        run_id="run_atomic_renew",
        asset_ids_by_medium=batch,
        diversity_keys_by_medium=diversity,
    )["portrait"][0]

    assert renewed.id == first.id
    assert renewed.status == "reserved"
    with db_session_factory() as session:
        rows = list(
            session.scalars(
                select(SelectionReservationRow).where(
                    SelectionReservationRow.run_id == "run_atomic_renew"
                )
            )
        )
    assert [(row.id, row.status) for row in rows] == [(first.id, "reserved")]
