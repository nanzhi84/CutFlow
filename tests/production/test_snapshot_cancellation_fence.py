from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    DigitalHumanVideoRequest,
    FinishedVideo,
    Job,
    JobStatus,
    JobType,
    NodeRun,
    NodeStatus,
    ProviderInvocation,
    ProviderStatus,
    PublishPackage,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage import Repository
from packages.core.storage.database import (
    ArtifactRow,
    FinishedVideoRow,
    JobRow,
    NodeRunRow,
    OutboxEventRow,
    ProviderInvocationRow,
    PublishPackageRow,
    SelectionReservationRow,
    WorkflowRunRow,
    YieldFunnelEventRow,
)
from packages.core.workflow import NodeExecutionError
from packages.production import SqlAlchemyProductionRepository


def _job_and_run(suffix: str) -> tuple[Job, WorkflowRun]:
    job = Job(
        id=f"job_cancel_fence_{suffix}",
        type=JobType.digital_human_video,
        status=JobStatus.running,
        case_id="case_demo",
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="测试取消提交栅栏。",
            voice={"voice_id": "voice_demo_cn"},
        ),
    )
    run = WorkflowRun(
        id=f"run_cancel_fence_{suffix}",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    return job, run


def _delivery_snapshot(
    job: Job, run: WorkflowRun, suffix: str
) -> tuple[Job, WorkflowRun, Repository]:
    artifact_id = f"art_cancel_fence_{suffix}"
    node_run_id = f"nr_cancel_fence_{suffix}"
    finished_id = f"fv_cancel_fence_{suffix}"
    package_id = f"pkg_cancel_fence_{suffix}"
    artifact_ref = ArtifactRef(
        artifact_id=artifact_id,
        kind=ArtifactKind.video_final,
        uri=f"s3://cutagent/{artifact_id}.mp4",
    )
    repository = Repository()
    repository.artifacts[artifact_id] = Artifact(
        id=artifact_id,
        case_id=run.case_id,
        run_id=run.id,
        node_run_id=node_run_id,
        kind=ArtifactKind.video_final,
        uri=artifact_ref.uri,
        sha256="a" * 64,
        payload_schema="uri-only",
    )
    report_id = f"art_cancel_report_{suffix}"
    repository.artifacts[report_id] = Artifact(
        id=report_id,
        case_id=run.case_id,
        run_id=run.id,
        kind=ArtifactKind.run_report_debug,
        payload_schema="RunReport.v1",
        payload={"status": "cancelled"},
    )
    invocation_id = f"pinv_cancel_audit_{suffix}"
    repository.provider_invocations[invocation_id] = ProviderInvocation(
        id=invocation_id,
        case_id=run.case_id,
        run_id=run.id,
        provider_id="audit-provider",
        model_id="audit-model",
        provider_profile_id="audit-profile",
        capability_id="audit.capability",
        status=ProviderStatus.failed,
    )
    repository.node_runs[run.id] = [
        NodeRun(
            id=node_run_id,
            run_id=run.id,
            node_id="SubtitleAndBgmMix",
            node_version="v1",
            status=NodeStatus.succeeded,
            input_manifest_hash="manifest",
            output_artifact_ids=[artifact_id],
        )
    ]
    repository.finished_videos[finished_id] = FinishedVideo(
        id=finished_id,
        case_id="case_demo",
        run_id=run.id,
        title="已完成视频",
        video_artifact=artifact_ref,
    )
    repository.publish_packages[package_id] = PublishPackage(
        id=package_id,
        case_id="case_demo",
        source_finished_video_id=finished_id,
        video_artifact=artifact_ref,
        platform_defaults={"title": "发布标题"},
    )
    repository.create_event(
        "workflow.finished_video.created",
        "run",
        run.id,
        {"finished_video_id": finished_id, "publish_package_id": package_id},
        dedupe_key=f"finished-video-{suffix}",
    )
    repository.record_yield_funnel_event(
        job_id=job.id,
        run_id=run.id,
        event_type="finished_video_created",
        dedupe_key=f"yield-finished-video-{suffix}",
        finished_video_id=finished_id,
        publish_package_id=package_id,
    )
    repository.record_yield_funnel_event(
        job_id=job.id,
        run_id=run.id,
        event_type="node_succeeded",
        dedupe_key=f"yield-node-succeeded-{suffix}",
    )
    return (
        job.model_copy(
            update={"status": JobStatus.succeeded, "latest_finished_video_id": finished_id}
        ),
        run.model_copy(update={"status": RunStatus.succeeded}),
        repository,
    )


def _seed_running_run(
    repository: SqlAlchemyProductionRepository, job: Job, run: WorkflowRun
) -> None:
    repository.sync_workflow_snapshot(job=job, run=run, repository=Repository())


def test_cancellation_lock_wins_and_rejects_late_delivery_snapshot(db_session_factory):
    production = SqlAlchemyProductionRepository(db_session_factory)
    job, run = _job_and_run("cancel_first")
    _seed_running_run(production, job, run)
    stale_job, stale_run, stale_repository = _delivery_snapshot(job, run, "cancel_first")
    errors: list[BaseException] = []

    with db_session_factory() as cancellation_session:
        durable_run = cancellation_session.get(WorkflowRunRow, run.id, with_for_update=True)
        durable_run.status = RunStatus.cancelling.value
        durable_run.cancel_mode = "graceful"
        cancellation_session.flush()

        def sync_late_snapshot() -> None:
            try:
                production.sync_workflow_snapshot(
                    job=stale_job,
                    run=stale_run,
                    repository=stale_repository,
                )
            except BaseException as exc:  # pragma: no cover - assertion reports the error
                errors.append(exc)

        thread = threading.Thread(target=sync_late_snapshot)
        thread.start()
        time.sleep(0.1)
        assert thread.is_alive(), "late snapshot should wait for the cancellation row lock"
        cancellation_session.commit()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    with db_session_factory() as session:
        assert session.get(WorkflowRunRow, run.id).status == RunStatus.cancelling.value
        assert session.get(JobRow, job.id).status == JobStatus.running.value
        assert session.get(ArtifactRow, "art_cancel_fence_cancel_first") is None
        assert session.get(ArtifactRow, "art_cancel_report_cancel_first") is not None
        assert session.get(ProviderInvocationRow, "pinv_cancel_audit_cancel_first") is not None
        assert session.get(NodeRunRow, "nr_cancel_fence_cancel_first") is None
        assert session.get(FinishedVideoRow, "fv_cancel_fence_cancel_first") is None
        assert session.get(PublishPackageRow, "pkg_cancel_fence_cancel_first") is None
        assert (
            session.scalar(
                select(OutboxEventRow).where(
                    OutboxEventRow.dedupe_key == "finished-video-cancel_first"
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(YieldFunnelEventRow).where(
                    YieldFunnelEventRow.dedupe_key == "yield-finished-video-cancel_first"
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(YieldFunnelEventRow).where(
                    YieldFunnelEventRow.dedupe_key == "yield-node-succeeded-cancel_first"
                )
            )
            is None
        )


def test_committed_delivery_survives_later_cancellation_request(db_session_factory):
    production = SqlAlchemyProductionRepository(db_session_factory)
    job, run = _job_and_run("commit_first")
    _seed_running_run(production, job, run)
    _committed_job, _committed_run, committed_repository = _delivery_snapshot(
        job,
        run,
        "commit_first",
    )
    production.sync_workflow_snapshot(
        job=job,
        run=run,
        repository=committed_repository,
    )

    requested = production.request_run_cancellation(run.id, force=True)

    assert requested.status == RunStatus.cancelling
    assert production.run_cancel_mode(run.id) == "force"
    assert run.id in production.run_ids_with_cancelling()
    with db_session_factory() as session:
        assert session.get(ArtifactRow, "art_cancel_fence_commit_first") is not None
        assert session.get(NodeRunRow, "nr_cancel_fence_commit_first") is not None
        assert session.get(FinishedVideoRow, "fv_cancel_fence_commit_first") is not None
        assert session.get(PublishPackageRow, "pkg_cancel_fence_commit_first") is not None
        assert (
            session.scalar(
                select(OutboxEventRow).where(
                    OutboxEventRow.dedupe_key == "finished-video-commit_first"
                )
            )
            is not None
        )
        assert (
            session.scalar(
                select(YieldFunnelEventRow).where(
                    YieldFunnelEventRow.dedupe_key == "yield-finished-video-commit_first"
                )
            )
            is not None
        )


def test_admitted_run_cancels_immediately_and_force_mode_is_sticky(db_session_factory):
    production = SqlAlchemyProductionRepository(db_session_factory)
    job, run = _job_and_run("admitted")
    admitted_job = job.model_copy(update={"status": JobStatus.queued})
    admitted_run = run.model_copy(update={"status": RunStatus.admitted})
    _seed_running_run(production, admitted_job, admitted_run)

    assert production.requested_run_cancel_mode(run.id) is None
    cancelled = production.request_run_cancellation(run.id, force=True)
    repeated = production.request_run_cancellation(run.id, force=False)

    assert cancelled.status == RunStatus.cancelled
    assert repeated.status == RunStatus.cancelled
    assert production.run_cancel_mode(run.id) == "force"
    assert production.requested_run_cancel_mode("missing-run") is None
    with db_session_factory() as session:
        assert session.get(JobRow, job.id).status == JobStatus.cancelled.value
        durable_run = session.get(WorkflowRunRow, run.id)
        assert durable_run.finished_at is not None
        assert durable_run.cancel_requested_at is not None


def test_cancelling_missing_run_fails_explicitly(db_session_factory):
    production = SqlAlchemyProductionRepository(db_session_factory)

    with pytest.raises(NodeExecutionError, match="Run missing-run is missing"):
        production.request_run_cancellation("missing-run", force=False)


def test_cancelling_row_without_mode_defaults_to_graceful(db_session_factory):
    production = SqlAlchemyProductionRepository(db_session_factory)
    job, run = _job_and_run("legacy_cancel_mode")
    _seed_running_run(production, job, run)
    with db_session_factory() as session:
        durable_run = session.get(WorkflowRunRow, run.id)
        durable_run.status = RunStatus.cancelling.value
        durable_run.cancel_mode = None
        session.commit()

    assert production.requested_run_cancel_mode(run.id) == "graceful"


def test_cancellation_fence_allows_reservation_release_but_blocks_new_leases(
    db_session_factory,
):
    production = SqlAlchemyProductionRepository(db_session_factory)
    job, run = _job_and_run("reservation_cleanup")
    runtime = Repository()
    existing = runtime.reserve_selections(
        case_id="case_demo",
        run_id=run.id,
        medium="portrait",
        asset_ids=["asset_before_cancel"],
    )[0]
    production.sync_workflow_snapshot(job=job, run=run, repository=runtime)
    production.request_run_cancellation(run.id, force=False)

    runtime.release_run_reservations(run_id=run.id)
    late = runtime.reserve_selections(
        case_id="case_demo",
        run_id=run.id,
        medium="portrait",
        asset_ids=["asset_after_cancel"],
    )[0]
    production.sync_workflow_snapshot(job=job, run=run, repository=runtime)

    with db_session_factory() as session:
        assert session.get(SelectionReservationRow, existing.id).status == "released"
        assert session.get(SelectionReservationRow, late.id) is None


def test_cancelled_terminal_state_cannot_be_reverted_by_stale_snapshot(db_session_factory):
    production = SqlAlchemyProductionRepository(db_session_factory)
    job, run = _job_and_run("terminal")
    _seed_running_run(production, job, run)
    production.request_run_cancellation(run.id, force=False)
    production.sync_workflow_snapshot(
        job=job.model_copy(update={"status": JobStatus.cancelled}),
        run=run.model_copy(update={"status": RunStatus.cancelled}),
        repository=Repository(),
    )

    stale_job, stale_run, stale_repository = _delivery_snapshot(job, run, "terminal")
    production.sync_workflow_snapshot(
        job=stale_job,
        run=stale_run,
        repository=stale_repository,
    )

    with db_session_factory() as session:
        assert session.get(WorkflowRunRow, run.id).status == RunStatus.cancelled.value
        assert session.get(JobRow, job.id).status == JobStatus.cancelled.value
        assert session.get(ArtifactRow, "art_cancel_fence_terminal") is None
        assert session.get(FinishedVideoRow, "fv_cancel_fence_terminal") is None


def test_final_cancel_snapshot_cannot_reference_blocked_finished_video(db_session_factory):
    production = SqlAlchemyProductionRepository(db_session_factory)
    job, run = _job_and_run("cancelled_with_delivery")
    _seed_running_run(production, job, run)
    production.request_run_cancellation(run.id, force=False)
    delivery_job, _delivery_run, delivery_repository = _delivery_snapshot(
        job,
        run,
        "cancelled_with_delivery",
    )

    production.sync_workflow_snapshot(
        job=delivery_job.model_copy(update={"status": JobStatus.cancelled}),
        run=run.model_copy(update={"status": RunStatus.cancelled}),
        repository=delivery_repository,
    )

    with db_session_factory() as session:
        durable_job = session.get(JobRow, job.id)
        assert durable_job.status == JobStatus.cancelled.value
        assert durable_job.latest_finished_video_id is None
        assert session.get(WorkflowRunRow, run.id).status == RunStatus.cancelled.value
        assert session.get(ArtifactRow, "art_cancel_fence_cancelled_with_delivery") is None
        assert session.get(FinishedVideoRow, "fv_cancel_fence_cancelled_with_delivery") is None
        assert session.get(PublishPackageRow, "pkg_cancel_fence_cancelled_with_delivery") is None
