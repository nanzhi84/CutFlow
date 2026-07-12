"""Snapshot must not roll back durable provider-invocation progress (issue #193).

The Gateway persists submit/polling/terminal transitions the moment they happen. A
workflow snapshot built from a stale in-memory copy would otherwise merge an earlier
status over the durable row and drop its ``external_job_id``. Runs against real
Postgres so the snapshot's durable read sees the seeded advanced row.
"""

from __future__ import annotations

from packages.core.contracts import (
    DigitalHumanVideoRequest,
    Job,
    JobType,
    ProviderInvocation,
    ProviderStatus,
    RunStatus,
    WorkflowRun,
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


def _seed_durable_invocation(db_session_factory, *, inv_id, run_id, key, status, external_job_id):
    with db_session_factory() as session:
        session.add(
            ProviderInvocationRow(
                id=inv_id,
                idempotency_key=key,
                run_id=run_id,
                provider_id="acme",
                model_id="model",
                provider_profile_id="profile_1",
                capability_id="tts.speech",
                status=status,
                external_job_id=external_job_id,
            )
        )
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
