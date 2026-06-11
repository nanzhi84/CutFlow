from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    ArtifactKind,
    ArtifactRef,
    Artifact,
    AnnotationBatchRequest,
    CaseAgentRunRequest,
    CaseDetail,
    CasePerformanceResponse,
    CreateEditorHandoffRequest,
    CreateImportBatchRequest,
    CreateJianyingDraftRequest,
    CreativeFeatureVector,
    DegradationNotice,
    DigitalHumanVideoRequest,
    EditorHandoffPackageArtifact,
    ErrorCode,
    FinishedVideo,
    FinishedVideoDetail,
    ImportBatchReport,
    ImportBatchStatus,
    ImportRowResult,
    JianyingDraftPackageArtifact,
    Job,
    JobDetailResponse,
    JobStatus,
    JobType,
    MetricsImportRequest,
    NodeRun,
    NodeStatus,
    NodeError,
    OutboxEvent,
    PageResponse,
    PerformanceAttributionResponse,
    PerformanceMetricView,
    PerformanceObservation,
    PromptInvocation,
    ProviderInvocation,
    ProviderStatus,
    PublishBatchRequest,
    PublishDefaults,
    PublishPackage,
    PublishRecord,
    RunArtifactsResponse,
    RunCard,
    RunDebugReportArtifact,
    RunDetailResponse,
    RunPublicReportArtifact,
    RunReportResponse,
    RunStatus,
    UsageMeterRecord,
    VideoVersion,
    WorkflowRun,
    YieldFunnelEvent,
    utcnow,
)
from packages.core.storage import Repository
from packages.core.storage.database import (
    ArtifactRow,
    CaseRow,
    FinishedVideoRow,
    ImportBatchReportRow,
    JobRow,
    MediaAssetRow,
    NodeRunRow,
    OutboxEventRow,
    PerformanceObservationRow,
    PromptInvocationRow,
    PromptTemplateRow,
    PromptVersionRow,
    ProviderInvocationRow,
    ProviderPriceCatalogRow,
    ProviderPriceItemRow,
    PublishPackageRow,
    PublishRecordRow,
    ScriptVersionRow,
    UsageMeterRecordRow,
    WorkflowRunRow,
    VideoVersionRow,
    YieldFunnelEventRow,
)
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError


SUPPORTED_IMPORT_TYPES = {
    "case",
    "script",
    "media",
    "finished_video",
    "video_version",
    "publish_record",
    "performance",
    "prompt_seed",
    "provider_price",
}

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
DELETABLE_RUN_STATUSES = {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}


def _node_label(node_id: str | None) -> str | None:
    if not node_id:
        return None
    return NODE_LABELS.get(node_id, node_id)


def _run_progress(run: WorkflowRun, node_runs: list[NodeRun]) -> float:
    if run.status in {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}:
        return 1.0
    if not node_runs:
        return 0.05 if run.status in {RunStatus.created, RunStatus.admitted} else 0.1
    terminal = {NodeStatus.succeeded, NodeStatus.skipped, NodeStatus.degraded}
    completed = len([node for node in node_runs if node.status in terminal])
    running_bonus = 0.5 if any(node.status == NodeStatus.running for node in node_runs) else 0
    return min(0.95, max(0.05, (completed + running_bonus) / max(len(node_runs), 1)))


def _current_node_label(node_runs: list[NodeRun]) -> str | None:
    running = next((node for node in reversed(node_runs) if node.status == NodeStatus.running), None)
    if running is not None:
        return _node_label(running.node_id)
    latest = next((node for node in reversed(node_runs) if node.status != NodeStatus.pending), None)
    return _node_label(latest.node_id if latest else None)


def _run_title(job: Job) -> str:
    if isinstance(job.request, DigitalHumanVideoRequest):
        return job.request.title or job.request.script[:28] or job.id
    return job.id


def _run_warnings(node_runs: list[NodeRun]) -> list[str]:
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


def _run_has_retryable_failure(run: WorkflowRun, node_runs: list[NodeRun]) -> bool:
    if run.status != RunStatus.failed:
        return False
    return any(
        bool(node.error and node.error.retryable)
        for node in node_runs
        if node.status == NodeStatus.failed
    )


def _run_card_from_parts(
    *,
    run: WorkflowRun,
    job: Job,
    node_runs: list[NodeRun],
    has_finished_video: bool,
) -> RunCard:
    return RunCard(
        run_id=run.id,
        job_id=run.job_id,
        case_id=run.case_id or job.case_id or "",
        status=run.status,
        progress=_run_progress(run, node_runs),
        current_node_label=_current_node_label(node_runs),
        title=_run_title(job),
        warnings=_run_warnings(node_runs),
        can_resume=run.status == RunStatus.succeeded or _run_has_retryable_failure(run, node_runs),
        can_retry=run.status in {RunStatus.failed, RunStatus.cancelled},
        can_publish=run.status == RunStatus.succeeded and has_finished_video,
        started_at=run.started_at,
        updated_at=run.updated_at,
    )


def artifact_ref_from_row(row: ArtifactRow) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=row.id,
        kind=ArtifactKind(row.kind),
        uri=row.uri or f"artifact://{row.id}",
        schema_version=row.schema_version,
        sha256=row.sha256,
    )


def artifact_row_to_contract(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        case_id=row.case_id,
        run_id=row.run_id,
        node_run_id=row.node_run_id,
        kind=ArtifactKind(row.kind),
        uri=row.uri,
        local_path=row.local_path,
        oss_uri=row.oss_uri,
        size_bytes=row.size_bytes,
        immutable=row.immutable,
        retention_policy=row.retention_policy,
        sha256=row.sha256,
        media_info=row.media_info,
        payload_schema=row.payload_schema,
        payload=row.payload,
        created_by_node_run_id=row.created_by_node_run_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def case_row_to_contract(row: CaseRow) -> CaseDetail:
    return CaseDetail(
        id=row.id,
        name=row.name,
        owner_user_id=row.owner_user_id,
        status=row.status,
        description=row.description,
        industry=row.industry,
        product=row.product,
        target_audience=row.target_audience,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def job_row_to_contract(row: JobRow) -> Job:
    request_type = JobType(row.type)
    request = row.request
    if request_type == JobType.digital_human_video:
        request = DigitalHumanVideoRequest.model_validate(row.request)
    elif request_type == JobType.case_agent_run:
        request = CaseAgentRunRequest.model_validate(row.request)
    elif request_type == JobType.publish_batch:
        request = PublishBatchRequest.model_validate(row.request)
    elif request_type == JobType.annotation_batch:
        request = AnnotationBatchRequest.model_validate(row.request)
    return Job(
        id=row.id,
        type=request_type,
        status=JobStatus(row.status),
        case_id=row.case_id,
        created_by=row.created_by,
        request_schema=row.request_schema,
        request=request,
        active_run_id=row.active_run_id,
        latest_finished_video_id=row.latest_finished_video_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def workflow_run_row_to_contract(row: WorkflowRunRow) -> WorkflowRun:
    return WorkflowRun(
        id=row.id,
        job_id=row.job_id,
        case_id=row.case_id,
        workflow_template_id=row.workflow_template_id,
        workflow_version=row.workflow_version,
        status=RunStatus(row.status),
        requested_by=row.requested_by,
        run_attempt=row.run_attempt,
        resume_from_run_id=row.resume_from_run_id,
        retry_of_run_id=row.retry_of_run_id,
        experiment_assignment_id=row.experiment_assignment_id,
        public_report_artifact_id=row.public_report_artifact_id,
        debug_report_artifact_id=row.debug_report_artifact_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def node_run_row_to_contract(row: NodeRunRow) -> NodeRun:
    return NodeRun(
        id=row.id,
        run_id=row.run_id,
        node_id=row.node_id,
        node_version=row.node_version,
        status=NodeStatus(row.status),
        attempt=row.attempt,
        input_manifest_hash=row.input_manifest_hash,
        output_artifact_ids=list(row.output_artifact_ids or []),
        provider_invocation_ids=list(row.provider_invocation_ids or []),
        error=NodeError.model_validate(row.error) if row.error else None,
        skipped_reason=row.skipped_reason,
        degradation_reason=row.degradation_reason,
        warnings=list(row.warnings or []),
        degradations=[DegradationNotice.model_validate(item) for item in row.degradations or []],
        started_at=row.started_at,
        finished_at=row.finished_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def finished_video_row_to_contract(row: FinishedVideoRow) -> FinishedVideo:
    return FinishedVideo(
        id=row.id,
        case_id=row.case_id,
        run_id=row.run_id,
        title=row.title,
        video_artifact=ArtifactRef.model_validate(row.video_artifact),
        cover_artifact=ArtifactRef.model_validate(row.cover_artifact) if row.cover_artifact else None,
        subtitle_artifact=ArtifactRef.model_validate(row.subtitle_artifact) if row.subtitle_artifact else None,
        duration_sec=row.duration_sec,
        qc_status=row.qc_status,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def video_version_row_to_contract(row: VideoVersionRow) -> VideoVersion:
    return VideoVersion(
        id=row.id,
        case_id=row.case_id,
        script_version_id=row.script_version_id,
        finished_video_id=row.finished_video_id,
        timeline_plan_artifact_id=row.timeline_plan_artifact_id,
        style_plan_artifact_id=row.style_plan_artifact_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def publish_package_row_to_contract(row: PublishPackageRow) -> PublishPackage:
    return PublishPackage(
        id=row.id,
        case_id=row.case_id,
        source_finished_video_id=row.source_finished_video_id,
        upload_artifact_id=row.upload_artifact_id,
        video_artifact=ArtifactRef.model_validate(row.video_artifact),
        cover_artifact=ArtifactRef.model_validate(row.cover_artifact) if row.cover_artifact else None,
        platform_defaults=PublishDefaults.model_validate(row.platform_defaults),
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def publish_record_row_to_contract(row: PublishRecordRow) -> PublishRecord:
    return PublishRecord(
        id=row.id,
        case_id=row.case_id,
        video_version_id=row.video_version_id,
        publish_package_id=row.publish_package_id,
        publish_batch_id=row.publish_batch_id,
        platform=row.platform,
        status=row.status,
        cover_artifact_id=row.cover_artifact_id,
        published_at=row.published_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def performance_observation_row_to_contract(row: PerformanceObservationRow) -> PerformanceObservation:
    return PerformanceObservation(
        id=row.id,
        case_id=row.case_id,
        publish_record_id=row.publish_record_id,
        metric_name=row.metric_name,
        metric_value=row.metric_value,
        observed_at=row.observed_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def import_report_row_to_contract(row: ImportBatchReportRow) -> ImportBatchReport:
    return ImportBatchReport(
        batch_id=row.id,
        import_type=row.import_type,
        status=ImportBatchStatus(row.status),
        created_count=row.created_count,
        skipped_count=row.skipped_count,
        failed_count=row.failed_count,
        results=[ImportRowResult.model_validate(item) for item in row.results],
        mapping_artifact_id=row.mapping_artifact_id,
        request_id="stored",
    )


def _report_row(report: ImportBatchReport) -> ImportBatchReportRow:
    return ImportBatchReportRow(
        id=report.batch_id,
        import_type=report.import_type,
        status=report.status.value,
        created_count=report.created_count,
        skipped_count=report.skipped_count,
        failed_count=report.failed_count,
        results=[item.model_dump(mode="json") for item in report.results],
        mapping_artifact_id=report.mapping_artifact_id,
    )


class SqlAlchemyProductionRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def sync_workflow_snapshot(
        self,
        *,
        job: Job,
        run: WorkflowRun,
        repository: Repository,
    ) -> None:
        with self.session_factory() as session:
            session.merge(self._job_row(job))
            session.flush()

            run_artifacts = [artifact for artifact in repository.artifacts.values() if artifact.run_id == run.id]
            for artifact in run_artifacts:
                session.merge(self._artifact_row(artifact))
            session.flush()

            session.merge(self._workflow_run_row(run))
            session.flush()

            for node_run in repository.node_runs.get(run.id, []):
                session.merge(self._node_run_row(node_run))
            session.flush()

            provider_invocation_ids = set()
            for invocation in repository.provider_invocations.values():
                if invocation.run_id == run.id:
                    provider_invocation_ids.add(invocation.id)
                    session.merge(self._provider_invocation_row(invocation))
            session.flush()

            for usage in repository.usage_records.values():
                if usage.provider_invocation_id in provider_invocation_ids:
                    session.merge(self._usage_meter_record_row(usage))
            session.flush()

            for prompt_invocation in repository.prompt_invocations.values():
                if prompt_invocation.run_id == run.id:
                    session.merge(self._prompt_invocation_row(prompt_invocation))
            session.flush()

            for script in repository.scripts.values():
                if script.case_id == run.case_id:
                    session.merge(self._script_version_row(script))
            session.flush()

            finished_video_ids = set()
            for finished in repository.finished_videos.values():
                if finished.run_id == run.id:
                    finished_video_ids.add(finished.id)
                    session.merge(self._finished_video_row(finished))
            session.flush()

            for version in repository.video_versions.values():
                if version.finished_video_id in finished_video_ids:
                    session.merge(self._video_version_row(version))
            session.flush()

            for package in repository.publish_packages.values():
                if package.source_finished_video_id in finished_video_ids:
                    session.merge(self._publish_package_row(package))
            session.flush()

            for event in repository.outbox.values():
                if event.aggregate_type in {"run", "workflow_run"} and event.aggregate_id == run.id:
                    session.merge(self._outbox_event_row(event))
            for event in repository.yield_events.values():
                if getattr(event, "run_id", None) == run.id:
                    session.merge(self._yield_funnel_event_row(event, run.case_id))
            session.commit()

    def case_run_cards(self, *, case_id: str, request_id: str, limit: int = 50) -> PageResponse[RunCard] | None:
        with self.session_factory() as session:
            if session.get(CaseRow, case_id) is None:
                return None
            run_rows = list(
                session.scalars(
                    select(WorkflowRunRow)
                    .where(WorkflowRunRow.case_id == case_id)
                    .order_by(WorkflowRunRow.updated_at.desc())
                    .limit(limit)
                )
            )
            items: list[RunCard] = []
            for run_row in run_rows:
                job_row = session.get(JobRow, run_row.job_id)
                if job_row is None:
                    continue
                run = workflow_run_row_to_contract(run_row)
                node_runs = [
                    node_run_row_to_contract(row)
                    for row in session.scalars(
                        select(NodeRunRow)
                        .where(NodeRunRow.run_id == run.id)
                        .order_by(NodeRunRow.created_at.asc())
                    )
                ]
                has_finished_video = (
                    session.scalar(
                        select(FinishedVideoRow.id)
                        .where(FinishedVideoRow.run_id == run.id)
                        .limit(1)
                    )
                    is not None
                )
                items.append(
                    _run_card_from_parts(
                        run=run,
                        job=job_row_to_contract(job_row),
                        node_runs=node_runs,
                        has_finished_video=has_finished_video,
                    )
                )
            return PageResponse(items=items, total_hint=len(items), request_id=request_id)

    def run_exists(self, run_id: str) -> bool:
        with self.session_factory() as session:
            return session.get(WorkflowRunRow, run_id) is not None

    def hydrate_workflow_runtime_snapshot(self, repository: Repository, run_id: str) -> None:
        with self.session_factory() as session:
            run_row = session.get(WorkflowRunRow, run_id)
            if run_row is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, f"Run {run_id} is missing.")
            job_row = session.get(JobRow, run_row.job_id)
            if job_row is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, f"Job {run_row.job_id} is missing.")
            if run_row.case_id:
                case_row = session.get(CaseRow, run_row.case_id)
                if case_row is not None:
                    repository.cases[case_row.id] = case_row_to_contract(case_row)

            job = job_row_to_contract(job_row)
            run = workflow_run_row_to_contract(run_row)
            repository.jobs[job.id] = job
            repository.runs[run.id] = run
            run_ids = {run_id}
            if run.resume_from_run_id:
                source_row = session.get(WorkflowRunRow, run.resume_from_run_id)
                if source_row is not None:
                    source_run = workflow_run_row_to_contract(source_row)
                    repository.runs[source_run.id] = source_run
                    run_ids.add(source_run.id)
            node_runs = [
                node_run_row_to_contract(row)
                for row in session.scalars(select(NodeRunRow).where(NodeRunRow.run_id.in_(run_ids)))
            ]
            repository.node_runs[run_id] = [node for node in node_runs if node.run_id == run_id]
            if run.resume_from_run_id:
                repository.node_runs[run.resume_from_run_id] = [
                    node for node in node_runs if node.run_id == run.resume_from_run_id
                ]
            for artifact in session.scalars(select(ArtifactRow).where(ArtifactRow.run_id.in_(run_ids))):
                contract = artifact_row_to_contract(artifact)
                repository.artifacts[contract.id] = contract

    def job_detail(self, job_id: str, request_id: str) -> JobDetailResponse | None:
        with self.session_factory() as session:
            job = session.get(JobRow, job_id)
            if job is None:
                return None
            runs = [
                workflow_run_row_to_contract(row)
                for row in session.scalars(
                    select(WorkflowRunRow)
                    .where(WorkflowRunRow.job_id == job_id)
                    .order_by(WorkflowRunRow.created_at.asc())
                )
            ]
            latest_report_artifact_id = runs[-1].public_report_artifact_id if runs else None
            return JobDetailResponse(
                job=job_row_to_contract(job),
                runs=runs,
                latest_report_artifact_id=latest_report_artifact_id,
                request_id=request_id,
            )

    def run_detail(self, run_id: str, request_id: str) -> RunDetailResponse | None:
        with self.session_factory() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                return None
            node_runs = [
                node_run_row_to_contract(row)
                for row in session.scalars(
                    select(NodeRunRow)
                    .where(NodeRunRow.run_id == run_id)
                    .order_by(NodeRunRow.created_at.asc())
                )
            ]
            artifacts = [
                artifact_ref_from_row(row)
                for row in session.scalars(
                    select(ArtifactRow)
                    .where(ArtifactRow.run_id == run_id)
                    .order_by(ArtifactRow.created_at.asc())
                )
            ]
            return RunDetailResponse(
                run=workflow_run_row_to_contract(run),
                node_runs=node_runs,
                artifacts=artifacts,
                request_id=request_id,
            )

    def run_report(self, run_id: str, request_id: str) -> RunReportResponse | None:
        with self.session_factory() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None or not run.public_report_artifact_id:
                return None
            public = session.get(ArtifactRow, run.public_report_artifact_id)
            debug = session.get(ArtifactRow, run.debug_report_artifact_id) if run.debug_report_artifact_id else None
            if public is None:
                return None
            return RunReportResponse(
                public_report=RunPublicReportArtifact.model_validate(public.payload),
                debug_report=RunDebugReportArtifact.model_validate(debug.payload) if debug else None,
                request_id=request_id,
            )

    def run_artifacts(self, run_id: str, request_id: str) -> RunArtifactsResponse | None:
        with self.session_factory() as session:
            if session.get(WorkflowRunRow, run_id) is None:
                return None
            artifacts = [
                artifact_ref_from_row(row)
                for row in session.scalars(
                    select(ArtifactRow)
                    .where(ArtifactRow.run_id == run_id)
                    .order_by(ArtifactRow.created_at.asc())
                )
            ]
            return RunArtifactsResponse(run_id=run_id, artifacts=artifacts, request_id=request_id)

    def delete_run_record(self, run_id: str) -> bool:
        with self.session_factory() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                return False
            if RunStatus(run.status) not in DELETABLE_RUN_STATUSES:
                raise NodeExecutionError(
                    ErrorCode.validation_conflict,
                    "Processing runs cannot be deleted.",
                )
            job_id = run.job_id
            now = utcnow()
            node_ids = [
                row.id for row in session.scalars(select(NodeRunRow).where(NodeRunRow.run_id == run_id))
            ]

            for row in session.scalars(select(FinishedVideoRow).where(FinishedVideoRow.run_id == run_id)):
                row.run_id = None
                row.updated_at = now
            for row in session.scalars(select(ArtifactRow).where(ArtifactRow.run_id == run_id)):
                row.run_id = None
                if row.node_run_id in node_ids:
                    row.node_run_id = None
                row.updated_at = now
            if node_ids:
                for row in session.scalars(select(MediaAssetRow).where(MediaAssetRow.node_run_id.in_(node_ids))):
                    row.node_run_id = None
                    row.updated_at = now
            for row in session.scalars(select(ProviderInvocationRow).where(ProviderInvocationRow.run_id == run_id)):
                row.run_id = None
                if row.node_run_id in node_ids:
                    row.node_run_id = None
                row.updated_at = now
            for row in session.scalars(select(PromptInvocationRow).where(PromptInvocationRow.run_id == run_id)):
                row.run_id = None
                if row.node_run_id in node_ids:
                    row.node_run_id = None
                row.updated_at = now
            for row in session.scalars(select(YieldFunnelEventRow).where(YieldFunnelEventRow.run_id == run_id)):
                row.run_id = None

            for row in session.scalars(select(NodeRunRow).where(NodeRunRow.run_id == run_id)):
                session.delete(row)
            session.delete(run)

            job = session.get(JobRow, job_id)
            if job is not None:
                remaining_runs = list(
                    session.scalars(
                        select(WorkflowRunRow)
                        .where(WorkflowRunRow.job_id == job_id)
                        .where(WorkflowRunRow.id != run_id)
                        .order_by(WorkflowRunRow.created_at.asc())
                    )
                )
                if remaining_runs:
                    job.active_run_id = remaining_runs[-1].id
                    job.updated_at = now
                else:
                    session.delete(job)
            session.commit()
            return True

    def list_finished_videos(self, *, case_id: str, limit: int = 50) -> list[FinishedVideo]:
        with self.session_factory() as session:
            statement = (
                select(FinishedVideoRow)
                .where(FinishedVideoRow.case_id == case_id)
                .order_by(FinishedVideoRow.updated_at.desc())
                .limit(limit)
            )
            return [finished_video_row_to_contract(row) for row in session.scalars(statement)]

    def finished_video_detail(self, finished_video_id: str) -> FinishedVideoDetail | None:
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                return None
            version = session.scalar(
                select(VideoVersionRow)
                .where(VideoVersionRow.finished_video_id == finished_video_id)
                .order_by(VideoVersionRow.updated_at.desc())
                .limit(1)
            )
            records = []
            if version is not None:
                record_statement = (
                    select(PublishRecordRow)
                    .where(PublishRecordRow.video_version_id == version.id)
                    .order_by(PublishRecordRow.updated_at.desc())
                )
                records = [publish_record_row_to_contract(row) for row in session.scalars(record_statement)]
            return FinishedVideoDetail(
                finished_video=finished_video_row_to_contract(finished),
                video_version=video_version_row_to_contract(version) if version else None,
                publish_records=records,
            )

    def artifact_uri_for_finished_video(self, finished_video_id: str) -> str | None:
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                return None
            ref = ArtifactRef.model_validate(finished.video_artifact)
            artifact = session.get(ArtifactRow, ref.artifact_id)
            return artifact.uri if artifact is not None and artifact.uri else ""

    def delete_finished_video(self, finished_video_id: str) -> None:
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                return
            for package in session.scalars(
                select(PublishPackageRow).where(PublishPackageRow.source_finished_video_id == finished_video_id)
            ):
                package.source_finished_video_id = None
            for version in session.scalars(
                select(VideoVersionRow).where(VideoVersionRow.finished_video_id == finished_video_id)
            ):
                version.finished_video_id = None
            session.delete(finished)
            session.commit()

    def create_editor_handoff(
        self, finished_video_id: str, payload: CreateEditorHandoffRequest
    ) -> EditorHandoffPackageArtifact:
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, "Finished video is missing.")
            artifact = ArtifactRow(
                id=new_id("art"),
                case_id=finished.case_id,
                kind=ArtifactKind.editor_handoff.value,
                uri=f"sandbox://handoff/{finished_video_id}.zip",
                payload_schema="EditorHandoffPackageArtifact.v1",
                payload={"finished_video_id": finished_video_id, "format": payload.format},
            )
            session.add(artifact)
            session.commit()
            session.refresh(artifact)
            return EditorHandoffPackageArtifact(
                package_artifact=artifact_ref_from_row(artifact),
                manifest={"finished_video_id": finished_video_id, "format": payload.format},
            )

    def create_jianying_draft(
        self, finished_video_id: str, payload: CreateJianyingDraftRequest
    ) -> JianyingDraftPackageArtifact:
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, "Finished video is missing.")
            artifact = ArtifactRow(
                id=new_id("art"),
                case_id=finished.case_id,
                kind=ArtifactKind.jianying_draft.value,
                uri=f"sandbox://jianying/{finished_video_id}.zip",
                payload_schema="JianyingDraftPackageArtifact.v1",
                payload={"finished_video_id": finished_video_id, "template_id": payload.template_id},
            )
            session.add(artifact)
            session.commit()
            session.refresh(artifact)
            return JianyingDraftPackageArtifact(
                package_artifact=artifact_ref_from_row(artifact),
                draft_manifest={
                    "finished_video_id": finished_video_id,
                    "template_id": payload.template_id or "default",
                },
            )

    def case_performance(self, *, case_id: str, window: str = "7d") -> CasePerformanceResponse:
        with self.session_factory() as session:
            statement = (
                select(PerformanceObservationRow)
                .where(PerformanceObservationRow.case_id == case_id)
                .order_by(PerformanceObservationRow.observed_at.desc())
            )
            observations = [performance_observation_row_to_contract(row) for row in session.scalars(statement)]
        return CasePerformanceResponse(
            metrics=PerformanceMetricView(
                impressions=int(sum(item.metric_value for item in observations if item.metric_name == "impressions")),
                views=int(sum(item.metric_value for item in observations if item.metric_name == "views")),
                likes=int(sum(item.metric_value for item in observations if item.metric_name == "likes")),
            ),
            observations=observations,
        )

    def import_metrics(
        self, *, case_id: str, payload: MetricsImportRequest, request_id: str
    ) -> ImportBatchReport:
        results: list[ImportRowResult] = []
        created = 0
        failed = 0
        with self.session_factory() as session:
            for index, row in enumerate(payload.rows):
                if not isinstance(row, dict):
                    failed += 1
                    results.append(self._failed_row(index, "Import row must be an object."))
                    continue
                internal_id = new_id("perf")
                if not payload.dry_run:
                    session.add(
                        PerformanceObservationRow(
                            id=internal_id,
                            case_id=case_id,
                            publish_record_id=str(row.get("publish_record_id", "manual")),
                            metric_name=str(row.get("metric_name", "views")),
                            metric_value=float(row.get("metric_value", 0)),
                            observed_at=utcnow(),
                        )
                    )
                created += 1
                results.append(ImportRowResult(row_index=index, status="created", internal_id=internal_id))
            report = ImportBatchReport(
                batch_id=new_id("imp"),
                import_type="performance",
                status=ImportBatchStatus.completed if failed == 0 else ImportBatchStatus.partially_failed,
                created_count=created,
                skipped_count=0,
                failed_count=failed,
                results=results,
                request_id=request_id,
            )
            if not payload.dry_run:
                session.add(_report_row(report))
            session.commit()
            return report

    def performance_attribution(self, video_version_id: str) -> PerformanceAttributionResponse | None:
        with self.session_factory() as session:
            version = session.get(VideoVersionRow, video_version_id)
            if version is None:
                return None
            statement = (
                select(PerformanceObservationRow)
                .where(PerformanceObservationRow.case_id == version.case_id)
                .order_by(PerformanceObservationRow.observed_at.desc())
            )
            observations = [performance_observation_row_to_contract(row) for row in session.scalars(statement)]
            return PerformanceAttributionResponse(
                video_version_id=video_version_id,
                feature_vector=CreativeFeatureVector(broll_count=1),
                observations=observations,
                contributing_memories=[],
            )

    def create_import_batch(self, payload: CreateImportBatchRequest, request_id: str) -> ImportBatchReport | None:
        if payload.import_type not in SUPPORTED_IMPORT_TYPES:
            return None
        results: list[ImportRowResult] = []
        created = 0
        failed = 0
        with self.session_factory() as session:
            for index, row in enumerate(payload.rows or []):
                if not isinstance(row, dict):
                    failed += 1
                    results.append(self._failed_row(index, "Import row must be an object."))
                    continue
                internal_id = new_id(payload.import_type)
                if not payload.dry_run:
                    self._create_import_row(session, payload.import_type, internal_id, row)
                created += 1
                results.append(
                    ImportRowResult(
                        row_index=index,
                        status="created",
                        external_id=str(row.get("external_id")) if row.get("external_id") else None,
                        internal_id=internal_id,
                    )
                )
            report = ImportBatchReport(
                batch_id=new_id("imp"),
                import_type=payload.import_type,
                status=ImportBatchStatus.completed if failed == 0 else ImportBatchStatus.partially_failed,
                created_count=created,
                skipped_count=0,
                failed_count=failed,
                results=results,
                request_id=request_id,
            )
            if not payload.dry_run:
                session.add(_report_row(report))
            session.commit()
            return report

    def get_import_batch(self, batch_id: str) -> ImportBatchReport | None:
        with self.session_factory() as session:
            row = session.get(ImportBatchReportRow, batch_id)
            return import_report_row_to_contract(row) if row else None

    def _create_import_row(self, session: Session, import_type: str, internal_id: str, row: dict) -> None:
        if import_type == "case":
            session.add(
                CaseRow(
                    id=internal_id,
                    name=str(row.get("name", "Imported case")),
                    owner_user_id=str(row.get("owner_user_id", "usr_admin")),
                    status=str(row.get("status", "active")),
                    description=str(row.get("description", "")),
                    industry=str(row.get("industry")) if row.get("industry") else None,
                    product=str(row.get("product")) if row.get("product") else None,
                    target_audience=str(row.get("target_audience")) if row.get("target_audience") else None,
                )
            )
        elif import_type == "script":
            session.add(
                ScriptVersionRow(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    title=str(row.get("title", "Imported script")),
                    script=str(row.get("script", "")),
                    creative_intent_artifact_id=(
                        str(row.get("creative_intent_artifact_id"))
                        if row.get("creative_intent_artifact_id")
                        else None
                    ),
                    adopted_from_draft_id=str(row.get("adopted_from_draft_id"))
                    if row.get("adopted_from_draft_id")
                    else None,
                )
            )
        elif import_type == "media":
            tags = row.get("tags", [])
            session.add(
                MediaAssetRow(
                    id=internal_id,
                    case_id=str(row.get("case_id")) if row.get("case_id") else None,
                    title=str(row.get("title", "Imported media")),
                    kind=str(row.get("kind", "other")),
                    source_artifact_id=str(row.get("source_artifact_id"))
                    if row.get("source_artifact_id")
                    else None,
                    tags=[str(item) for item in tags] if isinstance(tags, list) else [],
                    annotation_status=str(row.get("annotation_status", "pending")),
                    usable=bool(row.get("usable", True)),
                )
            )
        elif import_type == "finished_video":
            case_id = str(row.get("case_id", "case_demo"))
            artifact = ArtifactRow(
                id=new_id("art"),
                case_id=case_id,
                kind=ArtifactKind.video_finished.value,
                uri=str(row.get("uri", f"sandbox://import/{internal_id}.mp4")),
                payload_schema="ImportedFinishedVideoArtifact.v1",
                payload={"external_id": row.get("external_id")},
            )
            session.add(artifact)
            session.flush()
            session.add(
                FinishedVideoRow(
                    id=internal_id,
                    case_id=case_id,
                    title=str(row.get("title", "Imported finished video")),
                    video_artifact=artifact_ref_from_row(artifact).model_dump(mode="json"),
                    duration_sec=float(row.get("duration_sec", 0)),
                    qc_status=str(row.get("qc_status", "pending")),
                )
            )
        elif import_type == "video_version":
            session.add(
                VideoVersionRow(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    script_version_id=str(row.get("script_version_id")) if row.get("script_version_id") else None,
                    finished_video_id=str(row.get("finished_video_id")) if row.get("finished_video_id") else None,
                    timeline_plan_artifact_id=str(row.get("timeline_plan_artifact_id", "imported")),
                    style_plan_artifact_id=str(row.get("style_plan_artifact_id", "imported")),
                )
            )
        elif import_type == "publish_record":
            status = str(row.get("status", "published"))
            session.add(
                PublishRecordRow(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    video_version_id=str(row.get("video_version_id")) if row.get("video_version_id") else None,
                    publish_package_id=str(row.get("publish_package_id")) if row.get("publish_package_id") else None,
                    publish_batch_id=str(row.get("publish_batch_id")) if row.get("publish_batch_id") else None,
                    platform=str(row.get("platform", "manual")),
                    status=status,
                    cover_artifact_id=str(row.get("cover_artifact_id")) if row.get("cover_artifact_id") else None,
                    published_at=utcnow() if status == "published" else None,
                )
            )
        elif import_type == "performance":
            session.add(
                PerformanceObservationRow(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    publish_record_id=str(row.get("publish_record_id", "manual")),
                    metric_name=str(row.get("metric_name", "views")),
                    metric_value=float(row.get("metric_value", 0)),
                    observed_at=utcnow(),
                )
            )
        elif import_type == "prompt_seed":
            template = PromptTemplateRow(
                id=internal_id,
                name=str(row.get("name", "Imported prompt")),
                purpose=str(row.get("purpose", "imported")),
                variables_schema_ref={
                    "schema_id": str(row.get("variables_schema_id", "imported.variables")),
                    "schema_version": str(row.get("variables_schema_version", "v1")),
                },
                output_schema_ref={
                    "schema_id": str(row.get("output_schema_id", "imported.output")),
                    "schema_version": str(row.get("output_schema_version", "v1")),
                },
                status="active",
            )
            session.add(template)
            session.flush()
            now = utcnow()
            session.add(
                PromptVersionRow(
                    id=new_id("pver"),
                    prompt_template_id=template.id,
                    content=str(row.get("content", "")),
                    status="published",
                    changelog=str(row.get("changelog")) if row.get("changelog") else None,
                    approved_at=now,
                    published_at=now,
                )
            )
        elif import_type == "provider_price":
            catalog = ProviderPriceCatalogRow(
                id=internal_id,
                provider_id=str(row.get("provider_id", "sandbox")),
                status=str(row.get("status", "published")),
                currency=str(row.get("currency", "CNY")),
            )
            session.add(catalog)
            session.flush()
            if row.get("unit_price") is not None:
                unit_price = row.get("unit_price")
                if not isinstance(unit_price, dict):
                    unit_price = {"currency": catalog.currency, "amount": float(unit_price)}
                session.add(
                    ProviderPriceItemRow(
                        id=new_id("price_item"),
                        catalog_id=catalog.id,
                        provider_id=catalog.provider_id,
                        model_id=str(row.get("model_id", "*")),
                        capability_id=str(row.get("capability_id", "*")),
                        unit=str(row.get("unit", "call")),
                        unit_price=unit_price,
                        active_from=utcnow(),
                        active_to=None,
                    )
                )

    def _job_row(self, job: Job) -> JobRow:
        return JobRow(
            id=job.id,
            type=job.type.value,
            status=job.status.value,
            case_id=job.case_id,
            created_by=job.created_by,
            request_schema=job.request_schema,
            request=job.request.model_dump(mode="json"),
            active_run_id=job.active_run_id,
            latest_finished_video_id=job.latest_finished_video_id,
            schema_version=job.schema_version,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    def _artifact_row(self, artifact: Artifact) -> ArtifactRow:
        return ArtifactRow(
            id=artifact.id,
            case_id=artifact.case_id,
            run_id=artifact.run_id,
            node_run_id=artifact.node_run_id,
            kind=artifact.kind.value,
            uri=artifact.uri,
            local_path=artifact.local_path,
            oss_uri=artifact.oss_uri,
            size_bytes=artifact.size_bytes,
            immutable=artifact.immutable,
            retention_policy=artifact.retention_policy,
            sha256=artifact.sha256,
            media_info=artifact.media_info.model_dump(mode="json") if artifact.media_info else None,
            payload_schema=artifact.payload_schema,
            payload=artifact.payload,
            created_by_node_run_id=artifact.created_by_node_run_id,
            schema_version=artifact.schema_version,
            created_at=artifact.created_at,
            updated_at=artifact.updated_at,
        )

    def _workflow_run_row(self, run: WorkflowRun) -> WorkflowRunRow:
        return WorkflowRunRow(
            id=run.id,
            job_id=run.job_id,
            case_id=run.case_id,
            workflow_template_id=run.workflow_template_id,
            workflow_version=run.workflow_version,
            status=run.status.value,
            requested_by=run.requested_by,
            run_attempt=run.run_attempt,
            resume_from_run_id=run.resume_from_run_id,
            retry_of_run_id=run.retry_of_run_id,
            experiment_assignment_id=run.experiment_assignment_id,
            public_report_artifact_id=run.public_report_artifact_id,
            debug_report_artifact_id=run.debug_report_artifact_id,
            started_at=run.started_at,
            finished_at=run.finished_at,
            schema_version=run.schema_version,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    def _node_run_row(self, node_run: NodeRun) -> NodeRunRow:
        return NodeRunRow(
            id=node_run.id,
            run_id=node_run.run_id,
            node_id=node_run.node_id,
            node_version=node_run.node_version,
            status=node_run.status.value,
            attempt=node_run.attempt,
            input_manifest_hash=node_run.input_manifest_hash,
            output_artifact_ids=node_run.output_artifact_ids,
            provider_invocation_ids=node_run.provider_invocation_ids,
            error=node_run.error.model_dump(mode="json") if node_run.error else None,
            skipped_reason=node_run.skipped_reason,
            degradation_reason=node_run.degradation_reason,
            warnings=[item.value if hasattr(item, "value") else str(item) for item in node_run.warnings],
            degradations=[item.model_dump(mode="json") for item in node_run.degradations],
            started_at=node_run.started_at,
            finished_at=node_run.finished_at,
            schema_version=node_run.schema_version,
            created_at=node_run.created_at,
            updated_at=node_run.updated_at,
        )

    def _provider_invocation_row(self, invocation: ProviderInvocation) -> ProviderInvocationRow:
        return ProviderInvocationRow(
            id=invocation.id,
            case_id=invocation.case_id,
            run_id=invocation.run_id,
            node_run_id=invocation.node_run_id,
            provider_id=invocation.provider_id,
            model_id=invocation.model_id,
            provider_profile_id=invocation.provider_profile_id,
            capability_id=invocation.capability_id,
            prompt_version_id=invocation.prompt_version_id,
            status=invocation.status.value,
            price_item_id=invocation.price_item_id,
            billing_status=invocation.billing_status,
            duration_ms=invocation.duration_ms,
            retry_count=invocation.retry_count,
            input_tokens=invocation.input_tokens,
            output_tokens=invocation.output_tokens,
            estimated_cost=invocation.estimated_cost.model_dump(mode="json") if invocation.estimated_cost else None,
            actual_cost=invocation.actual_cost.model_dump(mode="json") if invocation.actual_cost else None,
            request_artifact_id=invocation.request_artifact_id,
            response_artifact_id=invocation.response_artifact_id,
            external_job_id=invocation.external_job_id,
            error=invocation.error.model_dump(mode="json") if invocation.error else None,
            started_at=invocation.started_at,
            finished_at=invocation.finished_at,
            schema_version=invocation.schema_version,
            created_at=invocation.created_at,
            updated_at=invocation.updated_at,
        )

    def _usage_meter_record_row(self, usage: UsageMeterRecord) -> UsageMeterRecordRow:
        return UsageMeterRecordRow(
            id=usage.id,
            provider_invocation_id=usage.provider_invocation_id,
            provider_id=usage.provider_id,
            model_id=usage.model_id,
            capability_id=usage.capability_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            audio_seconds=usage.audio_seconds,
            video_seconds=usage.video_seconds,
            image_count=usage.image_count,
            provider_credits=usage.provider_credits,
            raw_usage=usage.raw_usage,
            schema_version=usage.schema_version,
            created_at=usage.created_at,
            updated_at=usage.updated_at,
        )

    def _prompt_invocation_row(self, invocation: PromptInvocation) -> PromptInvocationRow:
        return PromptInvocationRow(
            id=invocation.id,
            prompt_template_id=invocation.prompt_template_id,
            prompt_version_id=invocation.prompt_version_id,
            case_id=invocation.case_id,
            run_id=invocation.run_id,
            node_run_id=invocation.node_run_id,
            provider_invocation_id=invocation.provider_invocation_id,
            variables_artifact_id=invocation.variables_artifact_id,
            output_artifact_id=invocation.output_artifact_id,
            status=invocation.status,
            schema_version=invocation.schema_version,
            created_at=invocation.created_at,
            updated_at=invocation.updated_at,
        )

    def _outbox_event_row(self, event: OutboxEvent) -> OutboxEventRow:
        return OutboxEventRow(
            id=event.id,
            topic=event.topic,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            dedupe_key=event.dedupe_key,
            payload_schema=event.payload_schema,
            payload=event.payload,
            status=event.status,
            attempts=event.attempts,
            available_at=event.available_at,
            published_at=event.published_at,
            last_error=event.last_error,
            schema_version=event.schema_version,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )

    def _yield_funnel_event_row(
        self, event: YieldFunnelEvent, case_id: str | None
    ) -> YieldFunnelEventRow:
        return YieldFunnelEventRow(
            id=event.id,
            case_id=case_id,
            job_id=event.job_id,
            run_id=event.run_id,
            finished_video_id=event.finished_video_id,
            publish_package_id=event.publish_package_id,
            publish_attempt_id=event.publish_attempt_id,
            event_type=event.event_type,
            event_time=event.event_time,
            dedupe_key=event.dedupe_key,
            schema_version=event.schema_version,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )

    def _script_version_row(self, script: ScriptVersion) -> ScriptVersionRow:
        return ScriptVersionRow(
            id=script.id,
            case_id=script.case_id,
            title=script.title,
            script=script.script,
            creative_intent_artifact_id=script.creative_intent_artifact_id,
            adopted_from_draft_id=script.adopted_from_draft_id,
            schema_version=script.schema_version,
            created_at=script.created_at,
            updated_at=script.updated_at,
        )

    def _finished_video_row(self, finished: FinishedVideo) -> FinishedVideoRow:
        return FinishedVideoRow(
            id=finished.id,
            case_id=finished.case_id,
            run_id=finished.run_id,
            title=finished.title,
            video_artifact=finished.video_artifact.model_dump(mode="json"),
            cover_artifact=finished.cover_artifact.model_dump(mode="json") if finished.cover_artifact else None,
            subtitle_artifact=(
                finished.subtitle_artifact.model_dump(mode="json") if finished.subtitle_artifact else None
            ),
            duration_sec=finished.duration_sec,
            qc_status=finished.qc_status,
            schema_version=finished.schema_version,
            created_at=finished.created_at,
            updated_at=finished.updated_at,
        )

    def _video_version_row(self, version: VideoVersion) -> VideoVersionRow:
        return VideoVersionRow(
            id=version.id,
            case_id=version.case_id,
            script_version_id=version.script_version_id,
            finished_video_id=version.finished_video_id,
            timeline_plan_artifact_id=version.timeline_plan_artifact_id,
            style_plan_artifact_id=version.style_plan_artifact_id,
            schema_version=version.schema_version,
            created_at=version.created_at,
            updated_at=version.updated_at,
        )

    def _publish_package_row(self, package: PublishPackage) -> PublishPackageRow:
        return PublishPackageRow(
            id=package.id,
            case_id=package.case_id,
            source_finished_video_id=package.source_finished_video_id,
            upload_artifact_id=package.upload_artifact_id,
            video_artifact=package.video_artifact.model_dump(mode="json"),
            cover_artifact=package.cover_artifact.model_dump(mode="json") if package.cover_artifact else None,
            platform_defaults=package.platform_defaults.model_dump(mode="json"),
            schema_version=package.schema_version,
            created_at=package.created_at,
            updated_at=package.updated_at,
        )

    def _failed_row(self, index: int, message: str) -> ImportRowResult:
        return ImportRowResult(
            row_index=index,
            status="failed",
            error=NodeError(code=ErrorCode.validation_invalid_options, message=message),
        )
