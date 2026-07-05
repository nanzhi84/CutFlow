"""Task 2: every newly-created FinishedVideo / YieldFunnelEvent row carries the
correct ``owner_user_id`` at write time (not relying on the 0018 backfill).

Owner resolution priority mirrors the migration backfill:
``run_id -> run.job_id -> job.created_by`` then ``job_id -> job.created_by`` then
``finished_video_id -> finished_videos.owner_user_id``.
"""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from packages.core.contracts import (
    ArtifactKind,
    ArtifactRef,
    DigitalHumanVideoRequest,
    FinishedVideo,
    Job,
    JobStatus,
    JobType,
    RunStatus,
    WorkflowRun,
    utcnow,
)
from packages.core.observability.funnel import (
    persist_funnel_event_rows,
    resolve_event_owner,
)
from packages.core.storage.database import (
    FinishedVideoRow,
    JobRow,
    SelectionReservationRow,
    UserRow,
    WorkflowRunRow,
    YieldFunnelEventRow,
)
from packages.core.storage import Repository
from packages.production import SqlAlchemyProductionRepository


sqlite3.register_adapter(dict, json.dumps)
sqlite3.register_adapter(list, json.dumps)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # pragma: no cover - test shim
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):  # pragma: no cover - test shim
    return "JSON"


def _session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (
        UserRow.__table__,
        JobRow.__table__,
        WorkflowRunRow.__table__,
        FinishedVideoRow.__table__,
        YieldFunnelEventRow.__table__,
        SelectionReservationRow.__table__,
    ):
        table.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_chain(factory, *, owner: str) -> dict[str, str]:
    """Seed user -> job(created_by=owner) -> run -> finished_video chain."""
    with factory() as session:
        session.add(
            UserRow(
                id=owner,
                email=f"{owner}@example.com",
                display_name=owner,
                password_hash="x",
                role="operator",
                status="active",
            )
        )
        session.add(
            JobRow(
                id="job_1",
                type="digital_human_video",
                status="succeeded",
                case_id="case_1",
                created_by=owner,
                request_schema="DigitalHumanVideoRequest.v1",
                request={},
            )
        )
        session.add(
            WorkflowRunRow(
                id="run_1",
                job_id="job_1",
                case_id="case_1",
                workflow_template_id="digital_human_v2",
                workflow_version="1",
                status="succeeded",
                requested_by=owner,
            )
        )
        session.add(
            FinishedVideoRow(
                id="fv_1",
                case_id="case_1",
                run_id="run_1",
                title="t",
                video_artifact={
                    "artifact_id": "art_1",
                    "kind": "video_finished",
                    "uri": "s3://x",
                },
                duration_sec=0.0,
                qc_status="pending",
                owner_user_id=owner,
            )
        )
        session.commit()
    return {"job_id": "job_1", "run_id": "run_1", "finished_video_id": "fv_1"}


def test_resolve_event_owner_priority_run_then_job_then_finished_video():
    factory = _session_factory()
    ids = _seed_chain(factory, owner="usr_alice")
    with factory() as session:
        # run_id wins.
        assert resolve_event_owner(session, run_id=ids["run_id"], job_id=None, finished_video_id=None) == "usr_alice"
        # job_id fallback.
        assert resolve_event_owner(session, run_id=None, job_id=ids["job_id"], finished_video_id=None) == "usr_alice"
        # finished_video fallback.
        assert (
            resolve_event_owner(session, run_id=None, job_id=None, finished_video_id=ids["finished_video_id"])
            == "usr_alice"
        )
        # nothing resolvable -> None (broken chain stays NULL).
        assert resolve_event_owner(session, run_id="missing", job_id=None, finished_video_id=None) is None


def test_persist_funnel_event_rows_writes_owner_from_run():
    factory = _session_factory()
    ids = _seed_chain(factory, owner="usr_bob")
    persist_funnel_event_rows(
        factory,
        [
            {
                "event_type": "published",
                "dedupe_key": "run_1:published",
                "run_id": ids["run_id"],
                "case_id": "case_1",
                "event_time": utcnow(),
            }
        ],
    )
    with factory() as session:
        row = session.scalar(
            select(YieldFunnelEventRow).where(YieldFunnelEventRow.dedupe_key == "run_1:published")
        )
        assert row is not None
        assert row.owner_user_id == "usr_bob"


def test_persist_funnel_event_rows_owner_none_when_chain_broken():
    factory = _session_factory()
    persist_funnel_event_rows(
        factory,
        [{"event_type": "qc_failed", "dedupe_key": "orphan:qc_failed", "run_id": "no_such_run"}],
    )
    with factory() as session:
        row = session.scalar(
            select(YieldFunnelEventRow).where(YieldFunnelEventRow.dedupe_key == "orphan:qc_failed")
        )
        assert row is not None
        assert row.owner_user_id is None


def test_finished_video_contract_carries_owner_user_id():
    finished = FinishedVideo(
        id="fv_x",
        case_id="case_1",
        run_id="run_1",
        title="t",
        video_artifact=ArtifactRef(
            artifact_id="art_1", kind=ArtifactKind.video_finished, uri="s3://x"
        ),
        owner_user_id="usr_carol",
    )
    assert finished.owner_user_id == "usr_carol"


def test_sync_workflow_snapshot_backfills_finished_video_owner_from_run_job():
    factory = _session_factory()
    owner = "usr_frank"
    with factory() as session:
        session.add(
            UserRow(
                id=owner,
                email=f"{owner}@example.com",
                display_name=owner,
                password_hash="x",
                role="operator",
                status="active",
            )
        )
        session.commit()

    job = Job(
        id="job_sync",
        type=JobType.digital_human_video,
        status=JobStatus.succeeded,
        case_id="case_1",
        created_by=owner,
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_1",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
        ),
    )
    run = WorkflowRun(
        id="run_sync",
        job_id=job.id,
        case_id="case_1",
        workflow_template_id="digital_human_v2",
        workflow_version="1",
        status=RunStatus.succeeded,
        requested_by=owner,
    )
    runtime = Repository()
    runtime.finished_videos["fv_sync"] = FinishedVideo(
        id="fv_sync",
        case_id="case_1",
        run_id=run.id,
        title="t",
        video_artifact=ArtifactRef(
            artifact_id="art_1", kind=ArtifactKind.video_finished, uri="s3://x"
        ),
        owner_user_id=None,
    )

    SqlAlchemyProductionRepository(factory).sync_workflow_snapshot(
        job=job,
        run=run,
        repository=runtime,
    )

    with factory() as session:
        row = session.get(FinishedVideoRow, "fv_sync")
        assert row is not None
        assert row.owner_user_id == owner
    assert runtime.finished_videos["fv_sync"].owner_user_id == owner


def test_sync_workflow_snapshot_preserves_terminal_run_from_stale_snapshot():
    factory = _session_factory()
    finished_at = utcnow()
    with factory() as session:
        session.add(
            JobRow(
                id="job_stale",
                type="digital_human_video",
                status="running",
                case_id="case_1",
                created_by=None,
                request_schema="DigitalHumanVideoRequest.v1",
                request={},
            )
        )
        session.add(
            WorkflowRunRow(
                id="run_stale",
                job_id="job_stale",
                case_id="case_1",
                workflow_template_id="digital_human_v2",
                workflow_version="v1",
                status="cancelled",
                finished_at=finished_at,
            )
        )
        session.commit()

    job = Job(
        id="job_stale",
        type=JobType.digital_human_video,
        status=JobStatus.running,
        case_id="case_1",
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_1",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
        ),
    )
    stale_run = WorkflowRun(
        id="run_stale",
        job_id=job.id,
        case_id="case_1",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
        finished_at=None,
    )
    runtime = Repository()
    SqlAlchemyProductionRepository(factory).sync_workflow_snapshot(
        job=job,
        run=stale_run,
        repository=runtime,
    )

    with factory() as session:
        row = session.get(WorkflowRunRow, "run_stale")
        assert row is not None
        assert row.status == "cancelled"
        assert row.finished_at == finished_at.replace(tzinfo=None)


def test_export_node_sets_owner_from_run_requested_by():
    from packages.production.pipeline.nodes import export_finished_video

    owner = export_finished_video._resolve_owner_user_id(
        _FakeRun(requested_by="usr_dave", job_id="job_1"),
        _FakeRepo(jobs={}),
    )
    assert owner == "usr_dave"


def test_export_node_falls_back_to_job_created_by():
    from packages.production.pipeline.nodes import export_finished_video

    owner = export_finished_video._resolve_owner_user_id(
        _FakeRun(requested_by=None, job_id="job_1"),
        _FakeRepo(jobs={"job_1": _FakeJob(created_by="usr_erin")}),
    )
    assert owner == "usr_erin"


class _FakeRun:
    def __init__(self, *, requested_by, job_id):
        self.requested_by = requested_by
        self.job_id = job_id


class _FakeJob:
    def __init__(self, *, created_by):
        self.created_by = created_by


class _FakeRepo:
    def __init__(self, *, jobs):
        self.jobs = jobs
