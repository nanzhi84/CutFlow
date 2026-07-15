from __future__ import annotations

import asyncio
import math
import time
from datetime import timedelta
from typing import Literal

from fastapi import Request, WebSocket, WebSocketDisconnect

from apps.api.common import (
    assert_owner_or_404,
    get_case,
    job_owner,
    production_repository,
    repository,
    request_id,
    run_owner,
    visible_owner_filter,
    workflow_runtime,
)
from apps.api.dependencies import current_user
from apps.api.services.auth import get_my_generation_defaults
from packages.core import contracts as c
from packages.core.observability.events import receive_from_subscriber
from packages.core.observability import replay_sqlalchemy_outbox
from packages.core.contracts.state_machines import (
    JOB_TRANSITIONS,
    RUN_TRANSITIONS,
    assert_transition,
)
from packages.core.storage.database import ArtifactRow, MediaAssetRow
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.core.observability import record_funnel_event, workflow_stage
from packages.core.observability.telemetry import (
    record_event_stream_connected,
    record_event_stream_disconnected,
    record_event_stream_heartbeat,
)
from packages.production.pipeline import ReusePlan, ReuseSourceRun, compute_reuse_plan
from packages.production.pipeline.digital_human import template_for
from packages.production.pipeline.node_sequence import expected_node_count


NODE_LABELS = {
    "ValidateRequest": "校验请求",
    "LoadCaseContext": "加载 Case 上下文",
    "ResolveCreativeIntent": "解析创作意图",
    "TTS": "生成配音",
    "MaterialPackPlanning": "规划素材包",
    "NarrationAlignment": "对齐旁白",
    "NarrationBoundaryPlanning": "规划旁白边界",
    "TimelineWindowPlanning": "编译时间线窗口",
    # Legacy display label for historical runs created before #158.
    "PortraitPlanning": "规划数字人镜头",
    "BrollPlanning": "规划 B-roll",
    "StylePlanning": "规划字幕与包装",
    "MediaSelectionAgentPlanning": "媒体选择 Agent 规划",
    "TimelineAssemblyValidation": "组装并校验时间线",
    "PortraitTrackBuild": "生成数字人轨道",
    "LipSync": "口型同步",
    "RenderFinalTimeline": "渲染主时间线",
    "CaptionCompositionPlanning": "规划固定字幕带",
    "BgmAgentPlanning": "规划背景音乐",
    "SubtitleAndBgmMix": "混合字幕与 BGM",
    "ExportFinishedVideo": "导出成片",
    "FinalizeRunReport": "生成 Run 报告",
}

DELETABLE_RUN_STATUSES = {c.RunStatus.succeeded, c.RunStatus.failed, c.RunStatus.cancelled}

# Server-side heartbeat cadence for the run event-stream WebSocket (issue #74).
# When no real event flows for this long, the server sends a lightweight
# heartbeat frame so intermediary proxies (relay VPS / tunnel / nginx) do not
# close the connection on idle timeout. Kept well under typical 30–60s proxy
# idle windows. Module-level so tests can shrink it.
EVENT_STREAM_HEARTBEAT_INTERVAL_SECONDS = 15.0
_RETIRED_EDITING_AGENT_TEMPLATE_ID = "digital_human_editing_agent_v1"


def _sync_workflow_snapshot(request: Request, run: c.WorkflowRun) -> None:
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
    if job.request.workflow_template_id == _RETIRED_EDITING_AGENT_TEMPLATE_ID:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            "digital_human_editing_agent_v1 已退役，不能新建、重试或续跑；"
            "请使用 digital_human_editing_agent_v2 创建新任务。",
            details={
                "workflow_template_id": job.request.workflow_template_id,
                "replacement_workflow_template_id": "digital_human_editing_agent_v2",
                "mode": mode,
                "source_run_id": from_run_id,
            },
        )
    template = template_for(job.request.workflow_template_id)
    if mode == "resume" and from_run_id is not None:
        failed_node = _failed_node(repo, from_run_id)
        active_node_ids = {node.node_id for node in template.nodes}
        if failed_node is not None and failed_node.node_id not in active_node_ids:
            raise NodeExecutionError(
                c.ErrorCode.validation_invalid_options,
                "历史 Run 的失败节点已从当前工作流移除，不能续跑；请重新发起任务。",
                details={
                    "source_run_id": from_run_id,
                    "failed_node_id": failed_node.node_id,
                    "workflow_template_id": template.workflow_template_id,
                },
            )
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
        and mode == "retry"
        and from_run_id is not None
        and repo.runs[from_run_id].status == c.RunStatus.failed
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
    # Funnel head: the run passes through created -> admitted in this one call, so
    # emit both §9.5 stages here (RunStatus.created -> "submitted",
    # RunStatus.admitted -> "admitted"); the run never lingers in ``created``.
    record_funnel_event(
        repo,
        event_type=workflow_stage(c.RunStatus.created),
        job_id=job_id,
        run_id=run.id,
        dedupe_aggregate_id=run.id,
        event_time=run.created_at,
    )
    record_funnel_event(
        repo,
        event_type=workflow_stage(c.RunStatus.admitted),
        job_id=job_id,
        run_id=run.id,
        dedupe_aggregate_id=run.id,
        event_time=run.updated_at,
    )
    return job, run, template


def _failed_node(repo, run_id: str) -> c.NodeRun | None:
    return next(
        (
            node
            for node in reversed(repo.node_runs.get(run_id, []))
            if node.status == c.NodeStatus.failed
        ),
        None,
    )


def _run_has_retryable_failure(repo, run_id: str) -> bool:
    if repo.runs[run_id].status != c.RunStatus.failed:
        return False
    run = repo.runs[run_id]
    if run.workflow_template_id == _RETIRED_EDITING_AGENT_TEMPLATE_ID:
        return False
    failed_node = _failed_node(repo, run_id)
    if failed_node is None or not failed_node.error or not failed_node.error.retryable:
        return False
    try:
        active_node_ids = {
            node.node_id for node in template_for(run.workflow_template_id).nodes
        }
    except NodeExecutionError:
        return False
    return failed_node.node_id in active_node_ids


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
    running = len([node for node in node_runs if node.status == c.NodeStatus.running])
    # Node runs are created lazily, so divide by the template's *total* node count,
    # not the count created so far (which would pin progress at ~95% immediately).
    total = max(expected_node_count(run.workflow_template_id), len(node_runs))
    return min(0.95, max(0.05, (completed + 0.5 * running) / max(total, 1)))


def _current_node_label(node_runs: list[c.NodeRun]) -> str | None:
    running = next(
        (node for node in reversed(node_runs) if node.status == c.NodeStatus.running), None
    )
    if running is not None:
        return _node_label(running.node_id)
    latest = next(
        (node for node in reversed(node_runs) if node.status != c.NodeStatus.pending), None
    )
    return _node_label(latest.node_id if latest else None)


def _run_title(job: c.Job, finished_video_title: str | None = None) -> str:
    # A completed run carries the generated headline on its finished video -> show it
    # instead of the persona-label request title or the raw script prefix. In-flight /
    # failed runs (no finished video yet) fall back to the request title / script prefix.
    if finished_video_title and finished_video_title.strip():
        return finished_video_title.strip()
    request_payload = job.request
    if isinstance(request_payload, c.DigitalHumanVideoRequest):
        return request_payload.title or request_payload.script[:28] or job.id
    return job.id


def _run_warnings(node_runs: list[c.NodeRun]) -> list[str]:
    values: list[str] = []
    for node in node_runs:
        values.extend(
            [
                warning.value if hasattr(warning, "value") else str(warning)
                for warning in node.warnings
            ]
        )
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
    finished_video = next(
        (video for video in repo.finished_videos.values() if video.run_id == run.id), None
    )
    has_finished_video = finished_video is not None
    can_resume = _run_has_retryable_failure(repo, run.id)
    return c.RunCard(
        run_id=run.id,
        job_id=run.job_id,
        case_id=run.case_id or job.case_id or "",
        status=run.status,
        progress=_run_progress(run, node_runs),
        current_node_label=_current_node_label(node_runs),
        title=_run_title(job, finished_video.title if finished_video else None),
        warnings=_run_warnings(node_runs),
        can_resume=can_resume,
        can_retry=run.status in {c.RunStatus.failed, c.RunStatus.cancelled},
        can_publish=run.status == c.RunStatus.succeeded and has_finished_video,
        started_at=run.started_at,
        updated_at=run.updated_at,
    )


def case_run_cards(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.RunCard]:
    # Creator-based isolation (spec §3): operator/viewer only see their own runs;
    # admin (owner_filter is None) sees all. The case itself stays shared.
    owner_filter = visible_owner_filter(current_user(request))
    if production_repository(request) is not None:
        response = production_repository(request).case_run_cards(
            case_id=case_id, request_id=request_id(), limit=limit, owner_user_id=owner_filter
        )
        if response is not None:
            return response
    get_case(request, case_id)
    repo = repository(request)
    runs = sorted(
        [
            run
            for run in repo.runs.values()
            if run.case_id == case_id and _run_visible(repo, run, owner_filter)
        ],
        key=lambda run: run.updated_at,
        reverse=True,
    )[:limit]
    return c.PageResponse(
        items=[_run_card(repo, run) for run in runs],
        total_hint=len(runs),
        request_id=request_id(),
    )


def _run_visible(repo, run: c.WorkflowRun, owner_filter: str | None) -> bool:
    """Whether ``run`` is visible under the creator-isolation owner filter.
    ``owner_filter is None`` -> admin, all visible. Otherwise only runs whose
    job.created_by matches (unowned runs are hidden from non-admins)."""
    if owner_filter is None:
        return True
    job = repo.jobs.get(run.job_id)
    owner = run.requested_by or (job.created_by if job is not None else None)
    return owner == owner_filter


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
    production_repository(request).hydrate_workflow_runtime_snapshot(repo, source_run_id)
    return compute_reuse_plan(
        ReuseSourceRun(
            run=repo.runs[source_run_id],
            node_runs=repo.node_runs.get(source_run_id, []),
        ),
        template,
        repo.artifacts,
    )


def _compensate_failed_start(request: Request, run_id: str, reason: str) -> None:
    """Mark a run (and its job) failed when its workflow could not be started.

    Guards against orphaned ``admitted`` runs: when ``start_run``/``resume_run``
    raises (e.g. Temporal unreachable / timed out) the run was already persisted
    as admitted but no workflow exists to drive it forward. We transition it to
    ``failed`` in place — guarded by the state-machine table so an illegal
    transition is never forced — fail its job, emit a failure event, and sync the
    snapshot. A run that already moved past ``admitted`` (local runtime that began
    executing) is left for its own terminal handling.
    """
    repo = repository(request)
    run = repo.runs.get(run_id)
    if run is None:
        return
    if c.RunStatus.failed not in RUN_TRANSITIONS.get(run.status, frozenset()):
        return
    run = run.model_copy(
        update={
            "status": c.RunStatus.failed,
            "finished_at": c.utcnow(),
            "updated_at": c.utcnow(),
        }
    )
    repo.runs[run_id] = run
    job = repo.jobs.get(run.job_id)
    if job is not None and c.JobStatus.failed in JOB_TRANSITIONS.get(job.status, frozenset()):
        repo.jobs[job.id] = job.model_copy(
            update={"status": c.JobStatus.failed, "updated_at": c.utcnow()}
        )
    repo.create_event(
        "workflow.run.failed",
        "run",
        run_id,
        {"job_id": run.job_id, "reason": reason, "status": c.RunStatus.failed.value},
        dedupe_key=f"{run_id}:run:{c.RunStatus.failed.value}",
        status=c.RunStatus.failed.value,
        message="Workflow failed to start.",
    )
    _sync_workflow_snapshot(request, repo.runs[run_id])


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
    try:
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
    except Exception as exc:
        # The run was admitted + persisted but the workflow could not be started.
        # Compensate to ``failed`` so it is not orphaned in ``admitted``, then
        # surface the failure to the caller instead of returning a stuck run.
        _compensate_failed_start(request, run.id, str(exc))
        if isinstance(exc, NodeExecutionError):
            raise
        raise NodeExecutionError(
            c.ErrorCode.workflow_worker_lost,
            f"Failed to start the workflow run; it was marked failed: {exc}",
        ) from exc
    _sync_workflow_snapshot(request, repository(request).runs[run.id])
    return repository(request).runs[run.id]


def _queue_submitted_run(
    request: Request,
    *,
    job_id: str,
    mode: str,
    from_run_id: str | None,
    reason: str | None,
) -> c.WorkflowRun:
    _job, run, _template = _admit_run(
        request,
        job_id=job_id,
        mode=mode,
        from_run_id=from_run_id,
        reason=reason,
    )
    _sync_workflow_snapshot(request, run)
    return repository(request).runs[run.id]


def _poke_case_admission(request: Request, case_id: str) -> None:
    runtime = workflow_runtime(request)
    signal = getattr(runtime, "signal_case_admission", None)
    if not callable(signal):
        return
    try:
        signal(case_id)
    except Exception:
        # The batch rows are durably queued. A failed poke should not turn a
        # successful batch-create request into a partial failure; the controller
        # will pick the queue up on a later signal/timer when Temporal is healthy.
        return


def _uses_case_admission_controller(request: Request) -> bool:
    return callable(getattr(workflow_runtime(request), "signal_case_admission", None))


def _runtime_run(request: Request, run_id: str) -> c.WorkflowRun:
    repo = repository(request)
    if run_id not in repo.runs:
        production_repository(request).hydrate_workflow_runtime_snapshot(repo, run_id)
    return repo.runs[run_id]


def _validate_seedance_reference_assets(
    request: Request, payload: c.DigitalHumanVideoRequest
) -> None:
    """Fail fast when Seedance reference asset ids are stale or not streamable.

    The Seedance node resolves each reference id to a source artifact before it
    calls the provider. Validating here keeps stale browser-local selections from
    creating an immediately failed workflow run.
    """
    if payload.workflow_template_id != "seedance_t2v_v1" or not payload.reference_asset_ids:
        return

    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    if session_factory is not None:
        with session_factory() as session:
            for asset_id in payload.reference_asset_ids:
                asset = session.get(MediaAssetRow, asset_id)
                if asset is None:
                    raise NodeExecutionError(
                        c.ErrorCode.validation_invalid_options,
                        f"Seedance reference asset is missing: {asset_id}",
                    )
                if asset.case_id not in {None, payload.case_id}:
                    raise NodeExecutionError(
                        c.ErrorCode.validation_invalid_options,
                        f"Seedance reference asset does not belong to this case: {asset_id}",
                    )
                if not asset.source_artifact_id:
                    raise NodeExecutionError(
                        c.ErrorCode.validation_invalid_options,
                        f"Seedance reference asset has no source artifact: {asset_id}",
                    )
                artifact = session.get(ArtifactRow, asset.source_artifact_id)
                if artifact is None or not artifact.uri:
                    raise NodeExecutionError(
                        c.ErrorCode.validation_invalid_options,
                        f"Seedance reference asset source URI is missing: {asset_id}",
                    )
        return

    repo = repository(request)
    for asset_id in payload.reference_asset_ids:
        asset = repo.media_assets.get(asset_id)
        if asset is None:
            raise NodeExecutionError(
                c.ErrorCode.validation_invalid_options,
                f"Seedance reference asset is missing: {asset_id}",
            )
        if asset.case_id not in {None, payload.case_id}:
            raise NodeExecutionError(
                c.ErrorCode.validation_invalid_options,
                f"Seedance reference asset does not belong to this case: {asset_id}",
            )
        if not asset.source_artifact_id:
            raise NodeExecutionError(
                c.ErrorCode.validation_invalid_options,
                f"Seedance reference asset has no source artifact: {asset_id}",
            )
        artifact = repo.artifacts.get(asset.source_artifact_id)
        if artifact is None or not artifact.uri:
            raise NodeExecutionError(
                c.ErrorCode.validation_invalid_options,
                f"Seedance reference asset source URI is missing: {asset_id}",
            )


def _validate_creative_intent_ref(request: Request, payload: c.DigitalHumanVideoRequest) -> None:
    ref = payload.creative_intent_ref
    if ref is None:
        return
    if ref.kind != c.ArtifactKind.creative_intent:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            "creative_intent_ref must declare kind creative.intent.",
        )

    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    if session_factory is not None:
        with session_factory() as session:
            artifact = session.get(ArtifactRow, ref.artifact_id)
            actual_kind = artifact.kind if artifact is not None else None
            actual_case_id = artifact.case_id if artifact is not None else None
    else:
        artifact = repository(request).artifacts.get(ref.artifact_id)
        actual_kind = artifact.kind.value if artifact is not None else None
        actual_case_id = artifact.case_id if artifact is not None else None

    if artifact is None:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            f"Creative intent artifact is missing: {ref.artifact_id}",
        )
    if actual_kind != c.ArtifactKind.creative_intent.value:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            f"Creative intent artifact has the wrong kind: {ref.artifact_id}",
        )
    if actual_case_id != payload.case_id:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            f"Creative intent artifact does not belong to this case: {ref.artifact_id}",
        )


def _validate_digital_human_request(request: Request, payload: c.DigitalHumanVideoRequest) -> None:
    _validate_seedance_reference_assets(request, payload)
    _validate_creative_intent_ref(request, payload)


def _validate_job_request_before_start(request: Request, job_id: str) -> None:
    job = repository(request).jobs[job_id]
    if isinstance(job.request, c.DigitalHumanVideoRequest):
        _validate_digital_human_request(request, job.request)


def create_digital_human_job(
    payload: c.CreateDigitalHumanVideoJobRequest, request: Request
) -> c.CreateJobResponse:
    case = get_case(request, payload.case_id)
    if payload.case_id not in repository(request).cases:
        repository(request).cases[payload.case_id] = case
    _validate_digital_human_request(request, payload)
    _link_adopted_script(request, payload)
    job = c.Job(
        id=new_id("job"),
        type=c.JobType.digital_human_video,
        case_id=payload.case_id,
        created_by=current_user(request).id,
        request_schema=payload.schema_version,
        request=payload,
    )
    repository(request).jobs[job.id] = job
    try:
        run = _start_submitted_run(
            request, job_id=job.id, mode="new", from_run_id=None, reason=None
        )
    except Exception:
        if not any(item.job_id == job.id for item in repository(request).runs.values()):
            repository(request).jobs.pop(job.id, None)
        raise
    return c.CreateJobResponse(
        job=repository(request).jobs[job.id], initial_run=run, request_id=request_id()
    )


# Option blocks that participate in the batch merge chain. Each maps to a field on
# both ``DigitalHumanVideoRequest`` (system default via default_factory) and the
# Optional blocks on ``UserGenerationDefaults`` / ``BatchItemOverrides``.
_OPTION_BLOCKS = (
    "voice",
    "broll",
    "lipsync",
    "subtitle",
    "bgm",
    "cover",
    "output",
    "strictness",
)


def merge_batch_item_options(
    overrides: c.BatchItemOverrides | None,
    my_defaults: c.UserGenerationDefaults | None,
) -> dict:
    """Merge a batch item's option blocks following the precedence
    ``item.overrides > my defaults > system default``.

    Returns a kwargs dict carrying only the blocks that are explicitly set by the
    item override or the user's saved defaults. Blocks absent from both are left
    out so ``DigitalHumanVideoRequest``'s default_factory supplies the system
    default. ``workflow_template_id`` (override-only) is included when present."""
    merged: dict = {}
    for block in _OPTION_BLOCKS:
        override_value = getattr(overrides, block, None) if overrides is not None else None
        default_value = getattr(my_defaults, block, None) if my_defaults is not None else None
        chosen = override_value if override_value is not None else default_value
        if chosen is not None:
            merged[block] = chosen
    if overrides is not None and overrides.workflow_template_id is not None:
        merged["workflow_template_id"] = overrides.workflow_template_id
    return merged


def _batch_item_request(
    payload: c.BatchDigitalHumanVideoRequest,
    item: c.BatchItem,
    my_defaults: c.UserGenerationDefaults | None,
) -> c.DigitalHumanVideoRequest:
    """Build the per-item ``DigitalHumanVideoRequest`` with the merged options."""
    merged = merge_batch_item_options(item.overrides, my_defaults)
    return c.DigitalHumanVideoRequest(
        case_id=payload.case_id,
        script=item.script,
        title=item.title,
        publish_content=item.publish_content or "",
        script_version_id=item.script_version_id,
        **merged,
    )


def create_digital_human_batch(
    payload: c.BatchDigitalHumanVideoRequest, request: Request
) -> c.BatchGenerationResponse:
    """Create one independent job+run per item, server-side, with per-item fault
    tolerance and merged defaults (plan Task 5).

    Merge precedence per item: ``item.overrides > my defaults > system default``.
    Each job is stamped ``created_by = current user``. Items are processed in
    order; a failing item is reported ``failed`` and does not abort the rest.
    Item-level idempotency keys ``{user.id}:{batch_key}:{index}`` make a replayed
    batch (same ``Idempotency-Key``) reuse the already-created jobs."""
    user = current_user(request)
    get_case(request, payload.case_id)
    my_defaults = get_my_generation_defaults(request) if payload.use_my_defaults else None
    batch_key = request.headers.get("Idempotency-Key") or new_id("batch")
    repo = repository(request)
    use_case_admission = _uses_case_admission_controller(request)
    created_status: Literal["created", "queued"] = "queued" if use_case_admission else "created"
    results: list[c.BatchItemResult] = []
    for index, item in enumerate(payload.items):
        idem_key = f"batch_item:{user.id}:{batch_key}:{index}"
        cached = repo.idempotency_records.get(idem_key)
        if cached is not None:
            results.append(
                c.BatchItemResult(
                    index=index,
                    job_id=cached.get("job_id"),
                    run_id=cached.get("run_id"),
                    status=created_status,
                )
            )
            continue
        try:
            item_request = _batch_item_request(payload, item, my_defaults)
            _validate_digital_human_request(request, item_request)
            _link_adopted_script(request, item_request)
            job = c.Job(
                id=new_id("job"),
                type=c.JobType.digital_human_video,
                case_id=payload.case_id,
                created_by=user.id,
                request_schema=item_request.schema_version,
                request=item_request,
            )
            repo.jobs[job.id] = job
            try:
                if use_case_admission:
                    run = _queue_submitted_run(
                        request, job_id=job.id, mode="new", from_run_id=None, reason=None
                    )
                else:
                    run = _start_submitted_run(
                        request, job_id=job.id, mode="new", from_run_id=None, reason=None
                    )
            except Exception:
                # Admission failed before a durable queue row existed; remove the
                # half-created draft job so the failed item leaves no orphan.
                if not any(r.job_id == job.id for r in repo.runs.values()):
                    repo.jobs.pop(job.id, None)
                raise
            repo.idempotency_records[idem_key] = {"job_id": job.id, "run_id": run.id}
            results.append(
                c.BatchItemResult(index=index, job_id=job.id, run_id=run.id, status=created_status)
            )
        except Exception as exc:  # noqa: BLE001 — per-item fault tolerance
            results.append(c.BatchItemResult(index=index, status="failed", error=str(exc)))
    if use_case_admission and any(result.status == "queued" for result in results):
        _poke_case_admission(request, payload.case_id)
    return c.BatchGenerationResponse(results=results, request_id=request_id())


def _parse_ids(ids: str | None) -> list[str] | None:
    if not ids:
        return None
    values = [item.strip() for item in ids.split(",") if item.strip()]
    return values or None


def run_overview(
    request: Request,
    *,
    limit: int = 50,
    cursor: str | None = None,
    status: c.RunStatus | None = None,
    ids: str | None = None,
) -> c.RunOverviewResponse:
    owner_filter = visible_owner_filter(current_user(request))
    run_ids = _parse_ids(ids)
    if production_repository(request) is not None:
        return production_repository(request).run_overview(
            request_id=request_id(),
            limit=limit,
            cursor=cursor,
            status=status,
            run_ids=run_ids,
            owner_user_id=owner_filter,
        )
    repo = repository(request)
    values = [
        run
        for run in repo.runs.values()
        if _run_visible(repo, run, owner_filter)
        and (status is None or run.status == status)
        and (not run_ids or run.id in run_ids)
    ]
    values.sort(key=lambda run: (run.updated_at, run.created_at), reverse=True)
    try:
        offset = max(0, int(cursor or "0"))
    except ValueError:
        offset = 0
    limit = max(1, min(100, int(limit or 50)))
    page_runs = values[offset : offset + limit]
    status_counts: dict[str, int] = {}
    failure_counts: dict[str, int] = {}
    degradation_counts: dict[str, int] = {}
    for run in values:
        status_counts[run.status.value] = status_counts.get(run.status.value, 0) + 1
        for node in repo.node_runs.get(run.id, []):
            if node.error is not None:
                failure_counts[node.error.code.value] = (
                    failure_counts.get(node.error.code.value, 0) + 1
                )
            for notice in node.degradations:
                code = notice.code.value if hasattr(notice.code, "value") else str(notice.code)
                degradation_counts[code] = degradation_counts.get(code, 0) + 1
    return c.RunOverviewResponse(
        items=[_run_card(repo, run) for run in page_runs],
        next_cursor=str(offset + limit) if offset + limit < len(values) else None,
        total_hint=len(values),
        status_counts=status_counts,
        failure_code_counts=failure_counts,
        degradation_code_counts=degradation_counts,
        request_id=request_id(),
    )


def batch_feasibility(
    request: Request,
    *,
    case_id: str,
    estimated_audio_duration_sec: float,
) -> c.BatchFeasibilityResponse:
    get_case(request, case_id)
    if production_repository(request) is not None:
        response = production_repository(request).batch_feasibility(
            case_id=case_id,
            estimated_audio_duration_sec=estimated_audio_duration_sec,
            request_id=request_id(),
        )
        if response is not None:
            return response
    repo = repository(request)
    audio_duration = max(0.0, float(estimated_audio_duration_sec or 0.0))
    video_assets = [
        asset
        for asset in repo.media_assets.values()
        if asset.usable
        and asset.case_id in {None, case_id}
        and asset.kind == "video"
        and asset.annotation_status == "annotated"
    ]
    portrait_assets = [
        asset
        for asset in video_assets
        if any(tag in {"portrait", "digital_human"} for tag in (asset.tags or []))
    ] or video_assets
    portrait_duration = sum(float(asset.duration_sec or 0.0) for asset in portrait_assets)
    estimated_windows = max(1, math.ceil(audio_duration / 4.0)) if audio_duration > 0 else 1
    notes: list[str] = []
    portrait_ok = (
        portrait_duration >= audio_duration if audio_duration > 0 else portrait_duration > 0
    )
    broll_ok = len(video_assets) >= estimated_windows
    if not portrait_ok:
        notes.append("portrait_duration_insufficient")
    if not broll_ok:
        notes.append("clean_broll_candidates_insufficient")
    if not video_assets:
        notes.append("no_annotated_video_material")
    return c.BatchFeasibilityResponse(
        case_id=case_id,
        estimated_audio_duration_sec=audio_duration,
        portrait_duration_sec=portrait_duration,
        clean_broll_candidate_count=len(video_assets),
        estimated_broll_window_count=estimated_windows,
        portrait_ok=portrait_ok,
        broll_ok=broll_ok,
        notes=notes,
        request_id=request_id(),
    )


def _link_adopted_script(request: Request, payload: c.DigitalHumanVideoRequest) -> None:
    """Persist the link to the adopted ScriptVersion referenced by the job request.

    The contract carries ``script_version_id`` on the request, which is stored on
    the job row inside the request payload. To stop the adopted ScriptVersion from
    being orphaned (and to preserve its ``adopted_from_draft_id`` provenance through
    the run snapshot), hydrate the existing ScriptVersion into the runtime repository
    so ExportFinishedVideo reuses it instead of fabricating a fresh row. We validate
    it belongs to the request's case; a cross-case or unknown id is ignored (the
    pipeline then mints a fresh ScriptVersion under that id, as before).
    """
    script_version_id = payload.script_version_id
    if not script_version_id:
        return
    repo = repository(request)
    existing = repo.scripts.get(script_version_id)
    if existing is None:
        existing = production_repository(request).hydrate_adopted_script(repo, script_version_id)
    if existing is not None and existing.case_id != payload.case_id:
        # Defensive: never relink a ScriptVersion across cases.
        repo.scripts.pop(script_version_id, None)


def job_detail(request: Request, job_id: str) -> c.JobDetailResponse:
    assert_owner_or_404(current_user(request), job_owner(request, job_id))
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
    _validate_job_request_before_start(request, job_id)
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
    assert_owner_or_404(current_user(request), run_owner(request, run_id))
    if production_repository(request) is not None:
        detail = production_repository(request).run_detail(run_id, request_id())
        if detail is not None:
            return detail
    run = repository(request).runs[run_id]
    node_runs = repository(request).node_runs.get(run_id, [])
    referenced_artifact_ids = {
        artifact_id for node_run in node_runs for artifact_id in node_run.output_artifact_ids
    }
    related_artifacts = [
        artifact
        for artifact in repository(request).artifacts.values()
        if artifact.run_id == run_id or artifact.id in referenced_artifact_ids
    ]
    artifacts = [repository(request).artifact_ref(artifact.id) for artifact in related_artifacts]
    payloads = {
        artifact.id: artifact.payload
        for artifact in related_artifacts
        if artifact.payload is not None
    }
    job = repository(request).jobs.get(run.job_id)
    config = c.build_run_config_summary(run_id, job) if job is not None else None
    return c.RunDetailResponse(
        run=run,
        node_runs=node_runs,
        artifacts=artifacts,
        artifact_payloads=payloads,
        config=config,
        request_id=request_id(),
    )


def cancel_run(run_id: str, payload: c.CancelRunRequest, request: Request) -> c.RunActionResponse:
    _runtime_run(request, run_id)
    run = workflow_runtime(request).cancel_run(run_id, force=payload.force, reason=payload.reason)
    run = run or repository(request).runs[run_id]
    repository(request).release_run_reservations(run_id=run_id, only_uncommitted=True)
    # Worker activities own fresh per-run repositories, so the API process may not
    # have the reservations created after its last hydration. Release the durable
    # rows by run id as well; this is idempotent and never touches committed picks.
    release_reservations = getattr(
        production_repository(request),
        "release_run_selection_reservations",
        None,
    )
    if callable(release_reservations):
        release_reservations(run_id)
    _sync_workflow_snapshot(request, run)
    return c.RunActionResponse(run=run, accepted=True, request_id=request_id())


def delete_run_record(run_id: str, request: Request) -> c.OkResponse:
    if run_id not in repository(request).runs:
        if not production_repository(request).run_exists(run_id):
            raise NodeExecutionError(c.ErrorCode.artifact_missing, f"Run {run_id} does not exist.")
        production_repository(request).hydrate_workflow_runtime_snapshot(
            repository(request), run_id
        )
    if run_id not in repository(request).runs:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, f"Run {run_id} does not exist.")
    run = repository(request).runs[run_id]
    if run.status not in DELETABLE_RUN_STATUSES:
        raise NodeExecutionError(
            c.ErrorCode.validation_conflict,
            "Processing runs cannot be deleted. Cancel or wait until the run reaches a terminal status.",
        )
    production_repository(request).delete_run_record(run_id)
    _delete_run_from_memory(repository(request), run)
    return c.OkResponse(request_id=request_id())


def _delete_run_from_memory(repo, run: c.WorkflowRun) -> None:
    repo.runs.pop(run.id, None)
    repo.node_runs.pop(run.id, None)
    for artifact_id, artifact in list(repo.artifacts.items()):
        if artifact.run_id == run.id:
            repo.artifacts[artifact_id] = artifact.model_copy(
                update={"run_id": None, "node_run_id": None}
            )
    for video_id, video in list(repo.finished_videos.items()):
        if video.run_id == run.id:
            repo.finished_videos[video_id] = video.model_copy(
                update={"run_id": None, "updated_at": c.utcnow()}
            )
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
        repo.jobs[job.id] = job.model_copy(
            update={"active_run_id": latest.id, "updated_at": c.utcnow()}
        )
    else:
        repo.jobs.pop(job.id, None)


def retry_run(run_id: str, payload: c.RetryRunRequest, request: Request) -> c.RetryRunResponse:
    run = _runtime_run(request, run_id)
    _validate_job_request_before_start(request, run.job_id)
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
    _validate_job_request_before_start(request, run.job_id)
    new_run = _start_submitted_run(
        request,
        job_id=run.job_id,
        mode="resume",
        # Lineage and artifact reuse are independent. Keep the real source run even
        # when the caller requests a clean recomputation; legacy v1 admission relies
        # on this provenance and the empty reuse plan still prevents artifact reuse.
        from_run_id=run_id,
        reason=payload.reason,
        reuse_valid_artifacts=payload.reuse_valid_artifacts,
    )
    return c.ResumeRunResponse(run=new_run, request_id=request_id())


def run_report(request: Request, run_id: str) -> c.RunReportResponse:
    assert_owner_or_404(current_user(request), run_owner(request, run_id))
    if production_repository(request) is not None:
        report = production_repository(request).run_report(run_id, request_id())
        if report is not None:
            return report
    run = repository(request).runs[run_id]
    if not run.public_report_artifact_id:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Run report is not available.")
    public_payload = repository(request).artifacts[run.public_report_artifact_id].payload
    debug_payload = (
        repository(request).artifacts[run.debug_report_artifact_id].payload
        if run.debug_report_artifact_id
        else None
    )
    return c.RunReportResponse(
        public_report=c.RunPublicReportArtifact.model_validate(public_payload),
        debug_report=c.RunDebugReportArtifact.model_validate(debug_payload)
        if debug_payload
        else None,
        request_id=request_id(),
    )


def run_artifacts(request: Request, run_id: str) -> c.RunArtifactsResponse:
    assert_owner_or_404(current_user(request), run_owner(request, run_id))
    if production_repository(request) is not None:
        response = production_repository(request).run_artifacts(run_id, request_id())
        if response is not None:
            return response
    refs = [
        repository(request).artifact_ref(item.id)
        for item in repository(request).artifacts.values()
        if item.run_id == run_id
    ]
    return c.RunArtifactsResponse(run_id=run_id, artifacts=refs, request_id=request_id())


def run_events(request: Request, run_id: str) -> c.EventStreamTokenResponse:
    assert_owner_or_404(current_user(request), run_owner(request, run_id))
    if not production_repository(request).run_exists(run_id):
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options, f"Run {run_id} does not exist."
        )
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
    sent_event_ids: set[str] = set()
    # #87 D2: a reconnecting client passes ?after=<last event_id> so replay resumes
    # just past what it already has, instead of re-sending the whole run history.
    after_event_id = websocket.query_params.get("after")
    session_factory = getattr(websocket.app.state, "sqlalchemy_session_factory", None)
    hub = websocket.app.state.event_hub
    # Initialize before the try so the finally can pair the connected gauge with
    # the disconnected gauge regardless of where the body unwinds: outbox replay,
    # a replay-time send_json raising WebSocketDisconnect, or hub.subscribe all
    # used to raise *outside* this try and leak event_stream_connections_active.
    subscriber = None
    record_event_stream_connected()
    try:
        replay_payloads = replay_sqlalchemy_outbox(
            session_factory,
            aggregate_type="run",
            aggregate_id=run_id,
            after_event_id=after_event_id,
        )
        for payload in replay_payloads:
            if payload.get("event_id"):
                sent_event_ids.add(str(payload["event_id"]))
            await websocket.send_json(payload)

        subscriber = hub.subscribe(run_id)
        last_send = time.monotonic()
        while True:
            payload = await receive_from_subscriber(subscriber)
            if payload is not None:
                event_id = payload.get("event_id")
                if event_id is not None and str(event_id) in sent_event_ids:
                    continue
                if event_id is not None:
                    sent_event_ids.add(str(event_id))
                await websocket.send_json(payload)
                last_send = time.monotonic()
                continue
            # Idle: emit a heartbeat so a relay/proxy does not close the
            # connection on idle timeout. The frontend ignores heartbeat frames
            # (they carry no event_id and are not added to the event list).
            if time.monotonic() - last_send >= EVENT_STREAM_HEARTBEAT_INTERVAL_SECONDS:
                await websocket.send_json(
                    {"event_type": "heartbeat", "server_time": c.utcnow().isoformat()}
                )
                record_event_stream_heartbeat()
                last_send = time.monotonic()
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=0.01)
            except TimeoutError:
                continue
            except WebSocketDisconnect:
                break
    finally:
        if subscriber is not None:
            hub.unsubscribe(run_id, subscriber)
        record_event_stream_disconnected()
