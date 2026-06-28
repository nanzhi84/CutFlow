from __future__ import annotations

from packages.core.contracts import (
    AnnotationBatchRequest,
    ArtifactKind,
    ArtifactRef,
    CaseDetail,
    DegradationNotice,
    DigitalHumanVideoRequest,
    FinishedVideo,
    ImportBatchReport,
    ImportBatchStatus,
    ImportRowResult,
    Job,
    JobStatus,
    JobType,
    NodeError,
    NodeRun,
    NodeStatus,
    CreativeFeatureVector,
    PublishBatchRequest,
    PublishRecord,
    RunStatus,
    VideoVersion,
    WorkflowRun,
)
from packages.core.storage.database import (
    ArtifactRow,
    CaseRow,
    CreativeFeatureVectorRow,
    FinishedVideoRow,
    ImportBatchReportRow,
    JobRow,
    NodeRunRow,
    PublishRecordRow,
    VideoVersionRow,
    WorkflowRunRow,
)
from packages.core.storage.performance_mappers import (
    performance_observation_row_to_contract as performance_observation_row_to_contract,
    performance_score_row_to_contract as performance_score_row_to_contract,
)
from packages.core.storage.sqlalchemy_uploads import artifact_row_to_contract as artifact_row_to_contract


def artifact_ref_from_row(row: ArtifactRow) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=row.id,
        kind=ArtifactKind(row.kind),
        uri=row.uri or f"artifact://{row.id}",
        schema_version=row.schema_version,
        sha256=row.sha256,
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
        video_number=row.video_number,
        video_artifact=ArtifactRef.model_validate(row.video_artifact),
        cover_artifact=ArtifactRef.model_validate(row.cover_artifact) if row.cover_artifact else None,
        subtitle_artifact=ArtifactRef.model_validate(row.subtitle_artifact) if row.subtitle_artifact else None,
        duration_sec=row.duration_sec,
        qc_status=row.qc_status,
        lipsync_provider_id=row.lipsync_provider_id,
        lipsync_fallback_used=row.lipsync_fallback_used,
        lipsync_fallback_reason=row.lipsync_fallback_reason,
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


def creative_feature_vector_row_to_contract(row: CreativeFeatureVectorRow) -> CreativeFeatureVector:
    return CreativeFeatureVector(
        id=row.id,
        case_id=row.case_id,
        script_version_id=row.script_version_id,
        video_version_id=row.video_version_id,
        hook_type=row.hook_type,
        script_structure=row.script_structure,
        topic_tags=list(row.topic_tags or []),
        cta_type=row.cta_type,
        angle=row.angle,
        duration_sec=row.duration_sec,
        broll_density=row.broll_density,
        cut_density=row.cut_density,
        subtitle_style_id=row.subtitle_style_id,
        bgm_id=row.bgm_id,
        cover_style=row.cover_style,
        material_ids=list(row.material_ids or []),
        broll_count=row.broll_count,
        title_tokens=row.title_tokens,
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
