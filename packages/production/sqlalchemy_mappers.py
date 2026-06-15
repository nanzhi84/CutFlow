from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

from packages.core.contracts import (
    AnnotationBatchRequest,
    Artifact,
    ArtifactKind,
    ArtifactRef,
    CaseAgentRunRequest,
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
    MediaInfo,
    NodeError,
    NodeRun,
    NodeStatus,
    CreativeFeatureVector,
    PerformanceObservation,
    PerformanceScore,
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
    PerformanceObservationRow,
    PerformanceScoreRow,
    PublishRecordRow,
    VideoVersionRow,
    WorkflowRunRow,
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


def performance_observation_row_to_contract(row: PerformanceObservationRow) -> PerformanceObservation:
    return PerformanceObservation(
        id=row.id,
        case_id=row.case_id,
        publish_record_id=row.publish_record_id,
        video_version_id=row.video_version_id,
        platform=row.platform,
        account_id=row.account_id,
        window=row.window,
        metric_name=row.metric_name,
        metric_value=row.metric_value,
        impressions=row.impressions,
        views=row.views,
        avg_watch_sec=row.avg_watch_sec,
        completion_rate=row.completion_rate,
        like_rate=row.like_rate,
        comment_rate=row.comment_rate,
        share_rate=row.share_rate,
        follow_rate=row.follow_rate,
        conversion_count=row.conversion_count,
        conversion_rate=row.conversion_rate,
        raw_metrics=dict(row.raw_metrics or {}),
        observed_at=row.observed_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def performance_score_row_to_contract(row: PerformanceScoreRow) -> PerformanceScore:
    return PerformanceScore(
        id=row.id,
        observation_id=row.observation_id,
        case_id=row.case_id,
        video_version_id=row.video_version_id,
        platform=row.platform,
        account_id=row.account_id,
        window=row.window,
        primary_metric=row.primary_metric,
        normalized_score=row.normalized_score,
        confidence=row.confidence,
        sample_size=row.sample_size,
        excluded_reason=row.excluded_reason,
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


def _media_info_from_import_metadata(
    *,
    uri: str,
    kind: str,
    content_type: str,
    duration_sec: float | None,
    width: int | None,
    height: int | None,
) -> MediaInfo | None:
    media_type = _media_type_from_metadata(kind, content_type)
    if media_type is None:
        return None
    suffix = Path(urlsplit(uri).path).suffix.lstrip(".")
    return MediaInfo(
        media_type=media_type,
        codec="unknown",
        format=suffix or content_type.split("/")[-1] or "unknown",
        mime_type=content_type,
        duration_sec=None if media_type == "image" else duration_sec,
        width=width,
        height=height,
    )


def _media_type_from_metadata(kind: str, content_type: str) -> str | None:
    if content_type.startswith("video/") or kind in {"portrait", "broll", "video"}:
        return "video"
    if content_type.startswith("audio/") or kind in {"bgm", "voice", "voice_reference"}:
        return "audio"
    if content_type.startswith("image/") or kind in {"image", "cover_template"}:
        return "image"
    return None


def _filename_from_uri(uri: str, *, fallback: str) -> str:
    filename = Path(unquote(urlsplit(uri).path)).name
    return filename or fallback or "imported-media"


def _optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
