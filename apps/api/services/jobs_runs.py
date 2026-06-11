from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from fastapi import Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from apps.api.common import (
    auth,
    case_learning_repository,
    case_repository,
    ensure_artifact_ref,
    get_case,
    media_repository,
    object_store,
    ops_repository,
    page,
    production_repository,
    prompt_repository,
    provider_repository,
    publishing_repository,
    repository,
    request_id,
    secret_repository,
    secret_store,
    signed,
    upload_repository,
    workflow_runtime,
)
from apps.api.dependencies import SESSION_COOKIE, current_user, not_found_response
from packages.core import contracts as c
from packages.core.observability.events import receive_from_subscriber
from packages.core.observability import replay_sqlalchemy_outbox
from packages.core.observability.outbox import OutboxWriter
from packages.core.auth import SqlAlchemyAuthService
from packages.core.contracts.state_machines import assert_transition
from packages.core.observability import metric_snapshot
from packages.core.registration_codes import hash_registration_code
from packages.core.storage.object_store import parse_local_uri
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline import ReusePlan, ReuseSourceRun, compute_reuse_plan
from packages.production.pipeline.digital_human import digital_human_template


def _sync_workflow_snapshot(request: Request, run: c.WorkflowRun) -> None:
    if production_repository(request) is not None:
        production_repository(request).sync_workflow_snapshot(
            job=repository(request).jobs[run.job_id],
            run=repository(request).runs[run.id],
            repository=repository(request),
        )


def _admit_run(
    request: Request,
    *,
    job_id: str,
    mode: str,
    from_run_id: str | None,
    reason: str | None,
) -> tuple[c.Job, c.WorkflowRun, c.WorkflowTemplate]:
    repo = repository(request)
    job = repo.jobs[job_id]
    next_job_status = job.status
    if next_job_status == c.JobStatus.draft:
        assert_transition("job", next_job_status, c.JobStatus.queued)
        next_job_status = c.JobStatus.queued
    elif (
        next_job_status == c.JobStatus.succeeded
        and mode == "resume"
        and from_run_id is not None
        and repo.runs[from_run_id].status == c.RunStatus.succeeded
    ):
        assert_transition("job", next_job_status, c.JobStatus.queued)
        next_job_status = c.JobStatus.queued
    elif (
        next_job_status == c.JobStatus.failed
        and mode == "resume"
        and from_run_id is not None
        and _run_has_retryable_failure(repo, from_run_id)
    ):
        assert_transition("job", next_job_status, c.JobStatus.queued)
        next_job_status = c.JobStatus.queued
    elif next_job_status not in {c.JobStatus.queued, c.JobStatus.running}:
        assert_transition("job", next_job_status, c.JobStatus.running)

    template = digital_human_template()
    attempt = 1 + len([run for run in repo.runs.values() if run.job_id == job_id])
    run = c.WorkflowRun(
        id=new_id("run"),
        job_id=job_id,
        case_id=job.case_id,
        workflow_template_id=template.workflow_template_id,
        workflow_version=template.version,
        status=c.RunStatus.created,
        requested_by=job.created_by,
        run_attempt=attempt,
        resume_from_run_id=from_run_id if mode == "resume" else None,
        retry_of_run_id=from_run_id if mode == "retry" else None,
    )
    assert_transition("run", run.status, c.RunStatus.admitted)
    run = run.model_copy(update={"status": c.RunStatus.admitted, "updated_at": c.utcnow()})
    repo.runs[run.id] = run
    repo.node_runs[run.id] = []
    job = job.model_copy(
        update={"active_run_id": run.id, "status": next_job_status, "updated_at": c.utcnow()}
    )
    repo.jobs[job.id] = job
    repo.create_event(
        "workflow.run.created",
        "run",
        run.id,
        {"job_id": job_id, "mode": mode, "reason": reason or ""},
        dedupe_key=f"{run.id}:run:{run.status.value}",
        status=run.status.value,
        message="Run admitted.",
    )
    return job, run, template


def _run_has_retryable_failure(repo, run_id: str) -> bool:
    if repo.runs[run_id].status != c.RunStatus.failed:
        return False
    return any(
        bool(node.error and node.error.retryable)
        for node in repo.node_runs.get(run_id, [])
        if node.status == c.NodeStatus.failed
    )


def _empty_reuse_plan(source_run_id: str, template: c.WorkflowTemplate) -> ReusePlan:
    first_node_id = template.nodes[0].node_id if template.nodes else None
    return ReusePlan(source_run_id=source_run_id, rerun_from_node_id=first_node_id)


def _compute_reuse_plan(
    request: Request,
    *,
    source_run_id: str,
    template: c.WorkflowTemplate,
    reuse_valid_artifacts: bool,
) -> ReusePlan:
    if not reuse_valid_artifacts:
        return _empty_reuse_plan(source_run_id, template)
    repo = repository(request)
    if production_repository(request) is not None:
        production_repository(request).hydrate_workflow_runtime_snapshot(repo, source_run_id)
    return compute_reuse_plan(
        ReuseSourceRun(
            run=repo.runs[source_run_id],
            node_runs=repo.node_runs.get(source_run_id, []),
        ),
        template,
        repo.artifacts,
    )


def _start_submitted_run(
    request: Request,
    *,
    job_id: str,
    mode: str,
    from_run_id: str | None,
    reason: str | None,
    reuse_valid_artifacts: bool = False,
) -> c.WorkflowRun:
    job, run, template = _admit_run(
        request,
        job_id=job_id,
        mode=mode,
        from_run_id=from_run_id,
        reason=reason,
    )
    _sync_workflow_snapshot(request, run)
    if mode == "resume" and from_run_id:
        workflow_runtime(request).resume_run(
            source_run_id=from_run_id,
            new_run=run,
            reuse_plan=_compute_reuse_plan(
                request,
                source_run_id=from_run_id,
                template=template,
                reuse_valid_artifacts=reuse_valid_artifacts,
            ),
        )
    else:
        workflow_runtime(request).start_run(job=job, run=run, template=template)
    _sync_workflow_snapshot(request, repository(request).runs[run.id])
    return repository(request).runs[run.id]


def _runtime_run(request: Request, run_id: str) -> c.WorkflowRun:
    repo = repository(request)
    if run_id not in repo.runs and production_repository(request) is not None:
        production_repository(request).hydrate_workflow_runtime_snapshot(repo, run_id)
    return repo.runs[run_id]

def create_digital_human_job(
    payload: c.CreateDigitalHumanVideoJobRequest, request: Request
) -> c.CreateJobResponse:
    case = get_case(request, payload.case_id)
    if payload.case_id not in repository(request).cases:
        repository(request).cases[payload.case_id] = case
    job = c.Job(
        id=new_id("job"),
        type=c.JobType.digital_human_video,
        case_id=payload.case_id,
        created_by="usr_admin",
        request_schema=payload.schema_version,
        request=payload,
    )
    repository(request).jobs[job.id] = job
    run = _start_submitted_run(request, job_id=job.id, mode="new", from_run_id=None, reason=None)
    return c.CreateJobResponse(job=repository(request).jobs[job.id], initial_run=run, request_id=request_id())


def job_detail(request: Request, job_id: str) -> c.JobDetailResponse:

    if production_repository(request) is not None:
        detail = production_repository(request).job_detail(job_id, request_id())
        if detail is not None:
            return detail
    job = repository(request).jobs[job_id]
    runs = [run for run in repository(request).runs.values() if run.job_id == job_id]
    return c.JobDetailResponse(
        job=job,
        runs=runs,
        latest_report_artifact_id=runs[-1].public_report_artifact_id if runs else None,
        request_id=request_id(),
    )


def create_run(job_id: str, payload: c.CreateRunRequest, request: Request) -> c.WorkflowRunResponse:
    previous = repository(request).jobs[job_id].active_run_id
    if previous is not None:
        _runtime_run(request, previous)
    run = _start_submitted_run(
        request,
        job_id=job_id,
        mode=payload.mode,
        from_run_id=previous if payload.mode in {"retry", "resume"} else None,
        reason=payload.reason,
        reuse_valid_artifacts=payload.mode == "resume",
    )
    return c.WorkflowRunResponse(run=run, request_id=request_id())


def run_detail(request: Request, run_id: str) -> c.RunDetailResponse:

    if production_repository(request) is not None:
        detail = production_repository(request).run_detail(run_id, request_id())
        if detail is not None:
            return detail
    run = repository(request).runs[run_id]
    node_runs = repository(request).node_runs.get(run_id, [])
    artifacts = [
        repository(request).artifact_ref(artifact.id) for artifact in repository(request).artifacts.values() if artifact.run_id == run_id
    ]
    return c.RunDetailResponse(run=run, node_runs=node_runs, artifacts=artifacts, request_id=request_id())


def cancel_run(run_id: str, payload: c.CancelRunRequest, request: Request) -> c.RunActionResponse:
    _runtime_run(request, run_id)
    run = workflow_runtime(request).cancel_run(run_id, force=payload.force, reason=payload.reason)
    run = run or repository(request).runs[run_id]
    _sync_workflow_snapshot(request, run)
    return c.RunActionResponse(run=run, accepted=True, request_id=request_id())


def retry_run(run_id: str, payload: c.RetryRunRequest, request: Request) -> c.RetryRunResponse:
    run = _runtime_run(request, run_id)
    new_run = _start_submitted_run(
        request,
        job_id=run.job_id,
        mode="retry",
        from_run_id=run_id,
        reason=payload.reason,
    )
    return c.RetryRunResponse(run=new_run, request_id=request_id())


def resume_run(run_id: str, payload: c.ResumeRunRequest, request: Request) -> c.ResumeRunResponse:
    run = _runtime_run(request, run_id)
    new_run = _start_submitted_run(
        request,
        job_id=run.job_id,
        mode="resume",
        from_run_id=run_id if payload.reuse_valid_artifacts else None,
        reason=payload.reason,
        reuse_valid_artifacts=payload.reuse_valid_artifacts,
    )
    return c.ResumeRunResponse(run=new_run, request_id=request_id())


def run_report(request: Request, run_id: str) -> c.RunReportResponse:

    if production_repository(request) is not None:
        report = production_repository(request).run_report(run_id, request_id())
        if report is not None:
            return report
    run = repository(request).runs[run_id]
    if not run.public_report_artifact_id:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Run report is not available.")
    public_payload = repository(request).artifacts[run.public_report_artifact_id].payload
    debug_payload = (
        repository(request).artifacts[run.debug_report_artifact_id].payload if run.debug_report_artifact_id else None
    )
    return c.RunReportResponse(
        public_report=c.RunPublicReportArtifact.model_validate(public_payload),
        debug_report=c.RunDebugReportArtifact.model_validate(debug_payload) if debug_payload else None,
        request_id=request_id(),
    )


def run_artifacts(request: Request, run_id: str) -> c.RunArtifactsResponse:

    if production_repository(request) is not None:
        response = production_repository(request).run_artifacts(run_id, request_id())
        if response is not None:
            return response
    refs = [repository(request).artifact_ref(item.id) for item in repository(request).artifacts.values() if item.run_id == run_id]
    return c.RunArtifactsResponse(run_id=run_id, artifacts=refs, request_id=request_id())


def run_events(request: Request, run_id: str) -> c.EventStreamTokenResponse:

    if production_repository(request) is not None and not production_repository(request).run_exists(run_id):
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, f"Run {run_id} does not exist.")
    token = request.app.state.event_tokens.issue(run_id, timedelta(minutes=10))
    return c.EventStreamTokenResponse(
        stream_url=f"/ws/runs/{run_id}",
        token=token.token,
        expires_at=token.expires_at,
        request_id=request_id(),
    )


async def run_websocket(websocket: WebSocket, run_id: str) -> None:
    token = websocket.query_params.get("token")
    if not token or not websocket.app.state.event_tokens.validate(token, run_id):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    repo = websocket.app.state.repository
    sent_event_ids: set[str] = set()
    session_factory = getattr(websocket.app.state, "sqlalchemy_session_factory", None)
    if session_factory is not None:
        replay_payloads = replay_sqlalchemy_outbox(
            session_factory,
            aggregate_type="run",
            aggregate_id=run_id,
        )
    else:
        writer = OutboxWriter.in_memory(repo)
        replay_payloads = [
            event.payload
            for event in writer.replay(aggregate_type="run", aggregate_id=run_id)
            if isinstance(event.payload, dict)
        ]
    for payload in replay_payloads:
        if payload.get("event_id"):
            sent_event_ids.add(str(payload["event_id"]))
        await websocket.send_json(payload)

    hub = websocket.app.state.event_hub
    subscriber = hub.subscribe(run_id)
    try:
        while True:
            payload = await receive_from_subscriber(subscriber)
            if payload is not None:
                event_id = payload.get("event_id")
                if event_id is not None and str(event_id) in sent_event_ids:
                    continue
                if event_id is not None:
                    sent_event_ids.add(str(event_id))
                await websocket.send_json(payload)
                continue
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=0.01)
            except TimeoutError:
                continue
            except WebSocketDisconnect:
                break
    finally:
        hub.unsubscribe(run_id, subscriber)
