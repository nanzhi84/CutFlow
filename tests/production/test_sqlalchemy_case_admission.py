from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from packages.core.contracts import JobStatus, JobType, RunStatus, utcnow
from packages.core.storage.database import (
    CaseRow,
    FailureTaxonomyRow,
    FinishedVideoRow,
    JobRow,
    MediaAssetRow,
    NodeRunRow,
    WorkflowRunRow,
)
from packages.core.storage import Repository
from packages.core.workflow import temporal_adapter
from packages.core.workflow.temporal_adapter import TemporalActivityContext
from packages.production import SqlAlchemyProductionRepository


def _request(script: str = "script") -> dict:
    return {
        "case_id": "case_admission_demo",
        "script": script,
        "voice": {"voice_id": "voice_demo_cn"},
    }


def test_case_admission_marks_running_only_after_temporal_start(db_session_factory):
    repository = SqlAlchemyProductionRepository(db_session_factory)
    now = utcnow()

    with db_session_factory() as session:
        session.add(
            CaseRow(
                id="case_admission_demo",
                name="Admission demo",
                owner_user_id="usr_admin",
                status="active",
                description=None,
            )
        )
        for index in range(2):
            job_id = f"job_admission_{index + 1}"
            run_id = f"run_admission_{index + 1}"
            session.add(
                JobRow(
                    id=job_id,
                    type=JobType.digital_human_video.value,
                    status=JobStatus.queued.value,
                    case_id="case_admission_demo",
                    created_by="usr_admin",
                    request_schema="DigitalHumanVideoRequest.v1",
                    request=_request(f"script {index + 1}"),
                    created_at=now + timedelta(seconds=index),
                    updated_at=now + timedelta(seconds=index),
                )
            )
            session.add(
                WorkflowRunRow(
                    id=run_id,
                    job_id=job_id,
                    case_id="case_admission_demo",
                    workflow_template_id="digital_human_v2",
                    workflow_version="v1",
                    status=RunStatus.admitted.value,
                    requested_by="usr_admin",
                    run_attempt=1,
                    created_at=now + timedelta(seconds=index),
                    updated_at=now + timedelta(seconds=index),
                )
            )
        session.commit()

    assert repository.case_ids_with_admitted_runs() == ["case_admission_demo"]

    summary = repository.admit_case_runs(case_id="case_admission_demo", max_inflight=1)

    assert [run.id for _job, run in summary["admitted"]] == ["run_admission_1"]
    with db_session_factory() as session:
        first = session.get(WorkflowRunRow, "run_admission_1")
        second = session.get(WorkflowRunRow, "run_admission_2")
        assert first is not None
        assert second is not None
        assert first.status == RunStatus.admitted.value
        assert first.started_at is None
        assert session.get(JobRow, "job_admission_1").status == JobStatus.queued.value
        assert second.status == RunStatus.admitted.value

    repository.mark_run_started("run_admission_1")

    with db_session_factory() as session:
        first = session.get(WorkflowRunRow, "run_admission_1")
        job = session.get(JobRow, "job_admission_1")
        assert first is not None
        assert job is not None
        assert first.status == RunStatus.running.value
        assert first.started_at is not None
        assert job.status == JobStatus.running.value
        assert job.active_run_id == "run_admission_1"


def test_admit_case_runs_takes_case_advisory_lock(db_session_factory):
    from sqlalchemy import event

    repository = SqlAlchemyProductionRepository(db_session_factory)
    now = utcnow()
    with db_session_factory() as session:
        session.add(
            CaseRow(
                id="case_lock",
                name="Lock demo",
                owner_user_id="usr_admin",
                status="active",
                description=None,
            )
        )
        session.add(
            JobRow(
                id="job_lock",
                type=JobType.digital_human_video.value,
                status=JobStatus.queued.value,
                case_id="case_lock",
                created_by="usr_admin",
                request_schema="DigitalHumanVideoRequest.v1",
                request=_request(),
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            WorkflowRunRow(
                id="run_lock",
                job_id="job_lock",
                case_id="case_lock",
                workflow_template_id="digital_human_v2",
                workflow_version="v1",
                status=RunStatus.admitted.value,
                requested_by="usr_admin",
                run_attempt=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    engine = db_session_factory.kw["bind"]
    executed_sql: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        executed_sql.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        summary = repository.admit_case_runs(case_id="case_lock", max_inflight=1)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    # The advisory lock statement is what serializes over-admission; assert it
    # actually reached the driver, and that the run was still selected.
    assert [run.id for _job, run in summary["admitted"]] == ["run_lock"]
    assert any("pg_advisory_xact_lock" in statement for statement in executed_sql)


def test_case_admission_start_failure_leaves_run_retryable(monkeypatch):
    run = SimpleNamespace(id="run_retry")
    job = SimpleNamespace(id="job_retry")

    class _ProductionRepository:
        def __init__(self) -> None:
            self.marked_started: list[str] = []

        def admit_case_runs(self, *, case_id: str, max_inflight: int):
            assert case_id == "case_retry"
            assert max_inflight == 1
            return {
                "admitted": [(job, run)],
                "active_count": 0,
                "queued_remaining": 1,
            }

        def mark_run_started(self, run_id: str) -> None:
            self.marked_started.append(run_id)

    production_repository = _ProductionRepository()
    monkeypatch.setattr(
        temporal_adapter,
        "_activity_context",
        TemporalActivityContext(
            repository=Repository(),
            local_runtime=SimpleNamespace(),
            production_repository=production_repository,
        ),
    )
    monkeypatch.setattr(
        temporal_adapter,
        "load_workflow_runtime_settings",
        lambda: SimpleNamespace(case_max_inflight_runs=1),
    )

    async def fail_start(_settings, *, job, run):
        raise RuntimeError("temporal temporarily unavailable")

    monkeypatch.setattr(temporal_adapter, "_start_admitted_workflow", fail_start)

    summary = temporal_adapter.admit_case_runs({"case_id": "case_retry"})

    assert summary == {
        "case_id": "case_retry",
        "admitted_run_ids": [],
        "start_error_run_ids": ["run_retry"],
        "active_count": 0,
        "queued_remaining": 1,
    }
    assert production_repository.marked_started == []


def test_run_overview_filters_and_aggregates_sql_rows(db_session_factory):
    repository = SqlAlchemyProductionRepository(db_session_factory)
    now = utcnow()
    with db_session_factory() as session:
        session.add(
            CaseRow(
                id="case_overview",
                name="Overview demo",
                owner_user_id="usr_admin",
                status="active",
                description=None,
            )
        )
        for index, (run_id, status, created_by) in enumerate(
            [
                ("run_overview_running", RunStatus.running.value, "usr_admin"),
                ("run_overview_hidden", RunStatus.failed.value, None),
                ("run_overview_failed", RunStatus.failed.value, "usr_admin"),
            ]
        ):
            job_id = f"job_overview_{index + 1}"
            session.add(
                JobRow(
                    id=job_id,
                    type=JobType.digital_human_video.value,
                    status=status,
                    case_id="case_overview",
                    created_by=created_by,
                    request_schema="DigitalHumanVideoRequest.v1",
                    request={**_request(f"overview {index + 1}"), "case_id": "case_overview"},
                    active_run_id=run_id if status == RunStatus.running.value else None,
                    created_at=now + timedelta(seconds=index),
                    updated_at=now + timedelta(seconds=index),
                )
            )
            session.add(
                WorkflowRunRow(
                    id=run_id,
                    job_id=job_id,
                    case_id="case_overview",
                    workflow_template_id="digital_human_v2",
                    workflow_version="v1",
                    status=status,
                    requested_by=created_by,
                    run_attempt=1,
                    created_at=now + timedelta(seconds=index),
                    updated_at=now + timedelta(seconds=index),
                )
            )
        session.flush()
        session.add(
            NodeRunRow(
                id="node_overview_failed",
                run_id="run_overview_failed",
                node_id="SubtitleAndBgmMix",
                node_version="v1",
                status="failed",
                input_manifest_hash="hash",
                degradations=[
                    {"code": "subtitle.burn_skipped", "message": "Subtitle burn skipped."},
                    {"code": "bgm.loudness_probe_failed", "message": "BGM probe failed."},
                ],
            )
        )
        session.add(
            FailureTaxonomyRow(
                id="failure_overview",
                target_type="run",
                target_id="run_overview_failed",
                failure_class="provider",
                error_code="provider_timeout",
                run_id="run_overview_failed",
                job_id="job_overview_3",
                case_id="case_overview",
                dedupe_key="failure_overview",
            )
        )
        session.add(
            FinishedVideoRow(
                id="fv_overview",
                case_id="case_overview",
                run_id="run_overview_failed",
                owner_user_id="usr_admin",
                title="Finished overview",
                video_number="1",
                video_artifact={"uri": "https://example.test/video.mp4"},
                cover_artifact={"uri": "https://example.test/cover.jpg"},
                duration_sec=12.0,
                qc_status="passed",
            )
        )
        session.commit()

    response = repository.run_overview(
        request_id="req_overview",
        limit=1,
        cursor="not-a-number",
        run_ids=["run_overview_running", "run_overview_failed", "run_overview_failed"],
        owner_user_id="usr_admin",
    )

    assert response.request_id == "req_overview"
    assert response.next_cursor == "1"
    assert response.total_hint == 2
    assert response.status_counts == {"failed": 1, "running": 1}
    assert response.failure_code_counts == {"provider_timeout": 1}
    assert response.degradation_code_counts == {
        "subtitle.burn_skipped": 1,
        "bgm.loudness_probe_failed": 1,
    }
    assert len(response.items) == 1
    assert response.items[0].run_id == "run_overview_failed"
    assert response.items[0].title == "Finished overview"
    assert response.items[0].can_retry is True

    failed_only = repository.run_overview(
        request_id="req_failed",
        status=RunStatus.failed,
        owner_user_id="usr_admin",
    )
    assert [item.run_id for item in failed_only.items] == ["run_overview_failed"]


def test_batch_feasibility_counts_annotated_materials(db_session_factory):
    repository = SqlAlchemyProductionRepository(db_session_factory)
    with db_session_factory() as session:
        session.add(
            CaseRow(
                id="case_feasibility",
                name="Feasibility demo",
                owner_user_id="usr_admin",
                status="active",
                description=None,
            )
        )
        session.add_all(
            [
                MediaAssetRow(
                    id="asset_portrait",
                    case_id="case_feasibility",
                    title="Portrait",
                    kind="video",
                    tags=["digital_human"],
                    annotation_status="annotated",
                    usable=True,
                    duration_sec=12.0,
                ),
                MediaAssetRow(
                    id="asset_broll",
                    case_id=None,
                    title="Global B-roll",
                    kind="video",
                    tags=["store"],
                    annotation_status="annotated",
                    usable=True,
                    duration_sec=4.0,
                ),
                MediaAssetRow(
                    id="asset_unusable",
                    case_id="case_feasibility",
                    title="Unusable",
                    kind="video",
                    tags=[],
                    annotation_status="annotated",
                    usable=False,
                    duration_sec=99.0,
                ),
            ]
        )
        session.commit()

    response = repository.batch_feasibility(
        case_id="case_feasibility",
        estimated_audio_duration_sec=8.0,
        request_id="req_feasible",
    )

    assert response is not None
    assert response.portrait_ok is True
    assert response.broll_ok is True
    assert response.portrait_duration_sec == 12.0
    assert response.clean_broll_candidate_count == 2
    assert response.estimated_broll_window_count == 2
    assert response.notes == []
    assert (
        repository.batch_feasibility(
            case_id="missing_case",
            estimated_audio_duration_sec=8.0,
            request_id="req_missing",
        )
        is None
    )
