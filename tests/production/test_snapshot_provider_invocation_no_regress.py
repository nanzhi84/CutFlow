"""Snapshot must not roll back durable provider-invocation progress (issue #193).

The Gateway persists submit/polling/terminal transitions the moment they happen. A
workflow snapshot built from a stale in-memory copy would otherwise merge an earlier
status over the durable row and drop its ``external_job_id``. Runs against real
Postgres so the snapshot's durable read sees the seeded advanced row.
"""

from __future__ import annotations

from datetime import timedelta

from packages.core.contracts import (
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    JobType,
    ProviderInvocation,
    ProviderStatus,
    RunStatus,
    WorkflowRun,
    utcnow,
)
from packages.core.provider_idempotency import build_provider_call_idempotency_key
from packages.core.storage.database import JobRow, ProviderInvocationRow, WorkflowRunRow
from packages.core.storage.repository import Repository, new_id
from packages.production import SqlAlchemyProductionRepository


def _seed_run(db_session_factory, run_id: str, job_id: str) -> tuple[Job, WorkflowRun]:
    with db_session_factory() as session:
        session.add(
            JobRow(
                id=job_id,
                type=JobType.digital_human_video.value,
                status="running",
                case_id="case_demo",
                created_by="usr_admin",
                request_schema="DigitalHumanVideoRequest.v1",
                request={
                    "case_id": "case_demo",
                    "script": "防回退快照测试。",
                    "voice": {"voice_id": "voice_demo_cn"},
                    "strictness": {"strict_timestamps": False},
                },
                active_run_id=run_id,
            )
        )
        session.add(
            WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                case_id="case_demo",
                workflow_template_id="digital_human_v2",
                workflow_version="v1",
                status=RunStatus.running.value,
                run_attempt=1,
                requested_by="usr_admin",
            )
        )
        session.commit()

    job = Job(
        id=job_id,
        type=JobType.digital_human_video,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="防回退快照测试。",
            voice={"voice_id": "voice_demo_cn"},
            strictness={"strict_timestamps": False},
        ),
    )
    run = WorkflowRun(
        id=run_id,
        job_id=job_id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    return job, run


def _seed_durable_invocation(
    db_session_factory,
    *,
    inv_id,
    run_id,
    key,
    status,
    external_job_id,
    error=None,
    finished_at=None,
    updated_at=None,
    result_payload=None,
):
    with db_session_factory() as session:
        row = ProviderInvocationRow(
            id=inv_id,
            idempotency_key=key,
            run_id=run_id,
            provider_id="acme",
            model_id="model",
            provider_profile_id="profile_1",
            capability_id="tts.speech",
            status=status,
            external_job_id=external_job_id,
            error=error,
            finished_at=finished_at,
            result_payload=result_payload,
        )
        if updated_at is not None:
            row.updated_at = updated_at
        session.add(row)
        session.commit()


def _stale_invocation(inv_id, run_id, key, status) -> ProviderInvocation:
    return ProviderInvocation(
        id=inv_id,
        idempotency_key=key,
        run_id=run_id,
        provider_id="acme",
        model_id="model",
        provider_profile_id="profile_1",
        capability_id="tts.speech",
        status=status,
    )


def _key() -> str:
    return build_provider_call_idempotency_key(
        run_id=new_id("run"),
        canonical_node_id="Tts",
        logical_call_slot="tts",
        provider_profile_id="profile_1",
        input_manifest_hash="manifest_1",
    )


def test_snapshot_does_not_regress_durable_polling_to_submitted(db_session_factory):
    run_id = new_id("run")
    inv_id = new_id("pinv")
    key = _key()
    job, run = _seed_run(db_session_factory, run_id, new_id("job"))
    _seed_durable_invocation(
        db_session_factory,
        inv_id=inv_id,
        run_id=run_id,
        key=key,
        status=ProviderStatus.polling.value,
        external_job_id="vendor-job-77",
    )

    repository = Repository()
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    # A stale copy that never saw the durable polling transition.
    repository.provider_invocations[inv_id] = _stale_invocation(
        inv_id, run_id, key, ProviderStatus.submitted
    )

    SqlAlchemyProductionRepository(db_session_factory).sync_workflow_snapshot(
        job=job, run=run, repository=repository
    )

    with db_session_factory() as session:
        row = session.get(ProviderInvocationRow, inv_id)
        assert row.status == ProviderStatus.polling.value
        assert row.external_job_id == "vendor-job-77"


def test_snapshot_preserves_durable_error_finished_at_and_updated_at(db_session_factory):
    # The Gateway wrote the terminal outcome (provider_submit_outcome_unknown) durably.
    # A stale in-memory copy still sitting on 'submitted' carries error=None /
    # finished_at=None / an older updated_at; merging it verbatim would erase the error
    # detail and rewind updated_at — which the Gateway's staleness check reads, so a
    # rewind makes a live holder look dead and invites a takeover.
    run_id = new_id("run")
    inv_id = new_id("pinv")
    key = _key()
    job, run = _seed_run(db_session_factory, run_id, new_id("job"))
    durable_finished = utcnow()
    durable_error = {
        "code": ErrorCode.provider_submit_outcome_unknown.value,
        "message": "Provider submit outcome is unknown after an interrupted attempt.",
        "retryable": False,
    }
    _seed_durable_invocation(
        db_session_factory,
        inv_id=inv_id,
        run_id=run_id,
        key=key,
        status=ProviderStatus.timed_out.value,
        external_job_id=None,
        error=durable_error,
        finished_at=durable_finished,
        updated_at=durable_finished,
    )

    repository = Repository()
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    stale = _stale_invocation(inv_id, run_id, key, ProviderStatus.submitted).model_copy(
        update={"updated_at": durable_finished - timedelta(minutes=5)}
    )
    repository.provider_invocations[inv_id] = stale

    SqlAlchemyProductionRepository(db_session_factory).sync_workflow_snapshot(
        job=job, run=run, repository=repository
    )

    with db_session_factory() as session:
        row = session.get(ProviderInvocationRow, inv_id)
        assert row.status == ProviderStatus.timed_out.value
        assert row.error is not None
        assert row.error["code"] == ErrorCode.provider_submit_outcome_unknown.value
        assert row.finished_at is not None
        # updated_at never rewinds past what the Gateway last committed.
        assert row.updated_at >= durable_finished


def test_snapshot_applies_forward_transition_to_terminal(db_session_factory):
    run_id = new_id("run")
    inv_id = new_id("pinv")
    key = _key()
    job, run = _seed_run(db_session_factory, run_id, new_id("job"))
    _seed_durable_invocation(
        db_session_factory,
        inv_id=inv_id,
        run_id=run_id,
        key=key,
        status=ProviderStatus.submitted.value,
        external_job_id=None,
    )

    repository = Repository()
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    # In-memory copy is ahead of the durable row: the terminal status must land.
    repository.provider_invocations[inv_id] = _stale_invocation(
        inv_id, run_id, key, ProviderStatus.succeeded
    )

    SqlAlchemyProductionRepository(db_session_factory).sync_workflow_snapshot(
        job=job, run=run, repository=repository
    )

    with db_session_factory() as session:
        row = session.get(ProviderInvocationRow, inv_id)
        assert row.status == ProviderStatus.succeeded.value


def test_snapshot_keeps_the_durable_result_payload(db_session_factory):
    # Nothing in the run state carries result_payload, so an unguarded merge would null
    # it out — and the next attempt of this node would find a succeeded row it cannot
    # replay, and pay the vendor again.
    run_id = new_id("run")
    inv_id = new_id("pinv")
    key = _key()
    job, run = _seed_run(db_session_factory, run_id, new_id("job"))
    envelope = {"result": {"output": {"ok": True}}, "usage": {"id": "usage_x"}}
    _seed_durable_invocation(
        db_session_factory,
        inv_id=inv_id,
        run_id=run_id,
        key=key,
        status=ProviderStatus.succeeded.value,
        external_job_id=None,
        result_payload=envelope,
    )

    repository = Repository()
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    repository.provider_invocations[inv_id] = _stale_invocation(
        inv_id, run_id, key, ProviderStatus.succeeded
    )

    SqlAlchemyProductionRepository(db_session_factory).sync_workflow_snapshot(
        job=job, run=run, repository=repository
    )

    with db_session_factory() as session:
        assert session.get(ProviderInvocationRow, inv_id).result_payload == envelope
