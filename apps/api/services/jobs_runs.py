from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING

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


NODE_LABELS = {
    "ValidateRequest": "校验请求",
    "LoadCaseContext": "加载 Case 上下文",
    "ResolveCreativeIntent": "解析创作意图",
    "TTS": "生成配音",
    "MaterialPackPlanning": "规划素材包",
    "NarrationAlignment": "对齐旁白",
    "PortraitPlanning": "规划数字人镜头",
    "BrollPlanning": "规划 B-roll",
    "StylePlanning": "规划字幕与包装",
    "TimelinePlanning": "规划时间线",
    "PortraitTrackBuild": "生成数字人轨道",
    "LipSync": "口型同步",
    "RenderFinalTimeline": "渲染主时间线",
    "SubtitleAndBgmMix": "混合字幕与 BGM",
    "ExportFinishedVideo": "导出成片",
    "FinalizeRunReport": "生成 Run 报告",
}

TTS_CAPABILITY_ID = "tts.speech"
VIDEO_CAPABILITY_ID = "lipsync.video"
TTS_UNIT = "input_token"
VIDEO_UNIT = "media_second"
DELETABLE_RUN_STATUSES = {c.RunStatus.succeeded, c.RunStatus.failed, c.RunStatus.cancelled}


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


def _node_label(node_id: str | None) -> str | None:
    if not node_id:
        return None
    return NODE_LABELS.get(node_id, node_id)


def _run_progress(run: c.WorkflowRun, node_runs: list[c.NodeRun]) -> float:
    if run.status in {c.RunStatus.succeeded, c.RunStatus.failed, c.RunStatus.cancelled}:
        return 1.0
    if not node_runs:
        return 0.05 if run.status in {c.RunStatus.created, c.RunStatus.admitted} else 0.1
    terminal = {c.NodeStatus.succeeded, c.NodeStatus.skipped, c.NodeStatus.degraded}
    completed = len([node for node in node_runs if node.status in terminal])
    running_bonus = 0.5 if any(node.status == c.NodeStatus.running for node in node_runs) else 0
    return min(0.95, max(0.05, (completed + running_bonus) / max(len(node_runs), 1)))


def _current_node_label(node_runs: list[c.NodeRun]) -> str | None:
    running = next((node for node in reversed(node_runs) if node.status == c.NodeStatus.running), None)
    if running is not None:
        return _node_label(running.node_id)
    latest = next((node for node in reversed(node_runs) if node.status != c.NodeStatus.pending), None)
    return _node_label(latest.node_id if latest else None)


def _run_title(job: c.Job) -> str:
    request_payload = job.request
    if isinstance(request_payload, c.DigitalHumanVideoRequest):
        return request_payload.title or request_payload.script[:28] or job.id
    return job.id


def _run_warnings(node_runs: list[c.NodeRun]) -> list[str]:
    values: list[str] = []
    for node in node_runs:
        values.extend([warning.value if hasattr(warning, "value") else str(warning) for warning in node.warnings])
        values.extend(
            [
                notice.code.value if hasattr(notice.code, "value") else str(notice.code)
                for notice in node.degradations
            ]
        )
    return sorted(set(values))


def _run_card(repo, run: c.WorkflowRun) -> c.RunCard:
    job = repo.jobs[run.job_id]
    node_runs = list(repo.node_runs.get(run.id, []))
    has_finished_video = any(video.run_id == run.id for video in repo.finished_videos.values())
    can_resume = run.status == c.RunStatus.succeeded or _run_has_retryable_failure(repo, run.id)
    return c.RunCard(
        run_id=run.id,
        job_id=run.job_id,
        case_id=run.case_id or job.case_id or "",
        status=run.status,
        progress=_run_progress(run, node_runs),
        current_node_label=_current_node_label(node_runs),
        title=_run_title(job),
        warnings=_run_warnings(node_runs),
        can_resume=can_resume,
        can_retry=run.status in {c.RunStatus.failed, c.RunStatus.cancelled},
        can_publish=run.status == c.RunStatus.succeeded and has_finished_video,
        started_at=run.started_at,
        updated_at=run.updated_at,
    )


def case_run_cards(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.RunCard]:
    if production_repository(request) is not None:
        response = production_repository(request).case_run_cards(case_id=case_id, request_id=request_id(), limit=limit)
        if response is not None:
            return response
    get_case(request, case_id)
    repo = repository(request)
    runs = sorted(
        [run for run in repo.runs.values() if run.case_id == case_id],
        key=lambda run: run.updated_at,
        reverse=True,
    )[:limit]
    return c.PageResponse(
        items=[_run_card(repo, run) for run in runs],
        total_hint=len(runs),
        request_id=request_id(),
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


def estimate_digital_human_video_cost(
    payload: c.CreateDigitalHumanVideoJobRequest, request: Request
) -> c.DigitalHumanVideoCostEstimateResponse:
    get_case(request, payload.case_id)
    tts_characters = len(payload.script.strip())
    estimated_video_seconds = max(
        1,
        int((Decimal(tts_characters) / Decimal("5")).to_integral_value(rounding=ROUND_CEILING)),
    )
    price_items = _active_price_items(request)
    tts = _estimate_line(
        label="TTS 字符",
        capability_id=TTS_CAPABILITY_ID,
        unit=TTS_UNIT,
        quantity=Decimal(tts_characters),
        price_items=price_items,
    )
    video = _estimate_line(
        label="视频秒数",
        capability_id=VIDEO_CAPABILITY_ID,
        unit=VIDEO_UNIT,
        quantity=Decimal(estimated_video_seconds),
        price_items=price_items,
    )
    total_amount = tts.estimated_cost.amount + video.estimated_cost.amount
    total = c.CostEstimateLine(
        label="总成本",
        capability_id="digital_human_video",
        unit="call",
        quantity=Decimal("1"),
        estimated_cost=c.Money(amount=total_amount, currency="CNY"),
        unpriced=tts.unpriced or video.unpriced,
    )
    return c.DigitalHumanVideoCostEstimateResponse(
        tts_characters=tts_characters,
        estimated_video_seconds=estimated_video_seconds,
        tts=tts,
        video=video,
        total=total,
        request_id=request_id(),
    )


def _active_price_items(request: Request) -> list[c.ProviderPriceItem]:
    provider_repo = provider_repository(request)
    if provider_repo is not None:
        catalogs = provider_repo.list_price_catalogs(active_only=True, limit=200)
        values: list[c.ProviderPriceItem] = []
        for catalog in catalogs:
            values.extend(provider_repo.list_price_items(catalog_id=catalog.id, limit=500))
        return values
    published_catalog_ids = {
        catalog.id for catalog in repository(request).price_catalogs.values() if catalog.status == "published"
    }
    return [item for item in repository(request).price_items.values() if item.catalog_id in published_catalog_ids]


def _estimate_line(
    *,
    label: str,
    capability_id: str,
    unit: str,
    quantity: Decimal,
    price_items: list[c.ProviderPriceItem],
) -> c.CostEstimateLine:
    price_item = next(
        (
            item
            for item in price_items
            if item.unit == unit and item.capability_id in {capability_id, "*"}
        ),
        None,
    )
    if price_item is None:
        return c.CostEstimateLine(
            label=label,
            capability_id=capability_id,
            quantity=quantity,
            unit=unit,
            estimated_cost=c.zero_money(),
            unpriced=True,
        )
    amount = price_item.unit_price.amount * quantity
    return c.CostEstimateLine(
        label=label,
        capability_id=capability_id,
        quantity=quantity,
        unit=unit,
        unit_price=price_item.unit_price,
        estimated_cost=c.Money(amount=amount, currency=price_item.unit_price.currency),
    )


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


def delete_run_record(run_id: str, request: Request) -> c.OkResponse:
    if run_id not in repository(request).runs and production_repository(request) is not None:
        if not production_repository(request).run_exists(run_id):
            raise NodeExecutionError(c.ErrorCode.artifact_missing, f"Run {run_id} does not exist.")
        production_repository(request).hydrate_workflow_runtime_snapshot(repository(request), run_id)
    if run_id not in repository(request).runs:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, f"Run {run_id} does not exist.")
    run = repository(request).runs[run_id]
    if run.status not in DELETABLE_RUN_STATUSES:
        raise NodeExecutionError(
            c.ErrorCode.validation_conflict,
            "Processing runs cannot be deleted. Cancel or wait until the run reaches a terminal status.",
        )
    if production_repository(request) is not None:
        production_repository(request).delete_run_record(run_id)
    _delete_run_from_memory(repository(request), run)
    return c.OkResponse(request_id=request_id())


def _delete_run_from_memory(repo, run: c.WorkflowRun) -> None:
    repo.runs.pop(run.id, None)
    repo.node_runs.pop(run.id, None)
    for artifact_id, artifact in list(repo.artifacts.items()):
        if artifact.run_id == run.id:
            repo.artifacts[artifact_id] = artifact.model_copy(update={"run_id": None, "node_run_id": None})
    for video_id, video in list(repo.finished_videos.items()):
        if video.run_id == run.id:
            repo.finished_videos[video_id] = video.model_copy(update={"run_id": None, "updated_at": c.utcnow()})
    for invocation_id, invocation in list(repo.provider_invocations.items()):
        if invocation.run_id == run.id:
            repo.provider_invocations[invocation_id] = invocation.model_copy(
                update={"run_id": None, "node_run_id": None, "updated_at": c.utcnow()}
            )
    job = repo.jobs.get(run.job_id)
    if job is None:
        return
    remaining_runs = sorted(
        [item for item in repo.runs.values() if item.job_id == job.id],
        key=lambda item: item.created_at,
    )
    if remaining_runs:
        latest = remaining_runs[-1]
        repo.jobs[job.id] = job.model_copy(update={"active_run_id": latest.id, "updated_at": c.utcnow()})
    else:
        repo.jobs.pop(job.id, None)


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
