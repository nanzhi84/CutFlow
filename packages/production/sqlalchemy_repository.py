from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import ARRAY, Float, Text, bindparam, case, cast, func, or_, select, text, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    ArtifactKind,
    ArtifactRef,
    CasePerformanceResponse,
    CreateEditorHandoffRequest,
    CreateImportBatchRequest,
    CreateJianyingDraftRequest,
    CreativeFeatureVector,
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
    JobStatus,
    JobDetailResponse,
    MediaInfo,
    MetricsImportRequest,
    NodeRun,
    NodeStatus,
    NodeError,
    OutboxEvent,
    PageResponse,
    PerformanceAttributionResponse,
    PerformanceMetricView,
    PromptInvocation,
    ProviderInvocation,
    PublishPackage,
    RunArtifactsResponse,
    RunCard,
    RunDebugReportArtifact,
    RunDetailResponse,
    RunOverviewResponse,
    BatchFeasibilityResponse,
    build_run_config_summary,
    RunPublicReportArtifact,
    RunReportResponse,
    RunStatus,
    ScriptVersion,
    SelectionLedgerEntry,
    SelectionReservationRecord,
    FailureTaxonomyEntry,
    UsageMeterRecord,
    VideoVersion,
    WorkflowRun,
    YieldFunnelEvent,
    normalize_publish_record_status,
    normalize_visual_asset_kind,
    utcnow,
)
from packages.core.contracts.artifacts import CaptionCompositionPlanArtifact, ClipEmbeddingRecord
from packages.core.observability.funnel import resolve_event_owner
from packages.core.observability.telemetry import record_artifact_commit_skipped_cancelled
from packages.core.storage import ObjectStore, Repository, get_object_store
from packages.core.storage.database import (
    AnnotationRow,
    ArtifactRow,
    CaseRow,
    ClipEmbeddingIndexRow,
    FinishedVideoRow,
    ImportBatchReportRow,
    JobRow,
    MediaAssetRow,
    NodeRunRow,
    OutboxEventRow,
    PerformanceObservationRow,
    PerformanceScoreRow,
    CreativeFeatureVectorRow,
    PromptInvocationRow,
    PromptTemplateRow,
    PromptVersionRow,
    ProviderInvocationRow,
    ProviderPriceCatalogRow,
    ProviderPriceItemRow,
    ProviderProfileRow,
    PublishPackageRow,
    PublishRecordRow,
    ScriptVersionRow,
    FailureTaxonomyRow,
    SelectionLedgerRow,
    SelectionReservationRow,
    UsageMeterRecordRow,
    Vector,
    VoiceProfileRow,
    WorkflowRunRow,
    VideoVersionRow,
    YieldFunnelEventRow,
)
from packages.core.storage.import_metadata import (
    imported_media_artifact_data,
    optional_float as _optional_float,
    optional_int as _optional_int,
    optional_str as _optional_str,
)
from packages.core.storage.performance_mappers import (
    performance_observation_to_row,
    performance_score_to_row,
)
from packages.core.storage.sqlalchemy_uploads import artifact_to_row
from packages.ai.gateway.sqlalchemy_repository import provider_profile_row_to_contract
from packages.creative.cases import evolution, metrics_import
from packages.creative.cases.sqlalchemy_learning_mappers import script_version_row_to_contract
from packages.core.storage.base_repository import BaseRepository
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.media.assets import local_object_path
from packages.production.pipeline.reuse import has_retryable_active_failure
from packages.media.sqlalchemy_repository import (
    annotation_row_to_editor,
    media_asset_row_to_contract,
    voice_row_to_contract,
)
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media
from packages.production.editor_handoff import EditorHandoffAsset, EditorHandoffBuilder, EditorHandoffInput
from packages.production.finished_video_numbering import next_finished_video_number
from packages.production.pipeline.node_sequence import expected_node_count
from packages.production.jianying_draft import (
    JianyingDraftBuilder,
    JianyingDraftInput,
    build_audio_segments_from_sources,
    build_text_segments_from_narration,
    build_video_segments_from_plans,
)
from packages.production.sqlalchemy_mappers import (
    artifact_ref_from_row,
    artifact_row_to_contract,
    case_row_to_contract,
    finished_video_row_to_contract,
    import_report_row_to_contract,
    job_row_to_contract,
    node_run_row_to_contract,
    performance_observation_row_to_contract,
    performance_score_row_to_contract,
    creative_feature_vector_row_to_contract,
    publish_record_row_to_contract,
    video_version_row_to_contract,
    workflow_run_row_to_contract,
    _report_row,
)


_TERMINAL_RUN_STATUS_VALUES = {
    RunStatus.succeeded.value,
    RunStatus.failed.value,
    RunStatus.cancelled.value,
}
_CANCELLATION_FENCE_STATUS_VALUES = {
    RunStatus.cancelling.value,
    RunStatus.cancelled.value,
}
_COMMITTED_NODE_STATUS_VALUES = {
    NodeStatus.succeeded.value,
    NodeStatus.degraded.value,
    NodeStatus.skipped.value,
}
_DELIVERY_ARTIFACT_KINDS = {
    ArtifactKind.video_rendered,
    ArtifactKind.video_final,
    ArtifactKind.video_finished,
    ArtifactKind.subtitle_ass,
    ArtifactKind.cover_image,
    ArtifactKind.cover_thumbnail,
    ArtifactKind.publish_package,
    ArtifactKind.editor_handoff,
    ArtifactKind.jianying_draft,
}
_CANCELLATION_BLOCKED_OUTBOX_TOPICS = {
    "ops.yield_funnel.event",
    "workflow.finished_video.created",
    "workflow.run.completed",
}


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

_FINISHED_VIDEO_NUMBER_RETRY_LIMIT = 3
_FINISHED_VIDEO_NUMBER_CONSTRAINT = "uq_finished_videos_case_video_number"
_SELECTION_RESERVATION_ACTIVE_SLOT_CONSTRAINT = "uq_selection_reservations_active_slot"

# Provider-invocation status order for the snapshot no-regression write. The Gateway
# writes prepared/submitted/polling/terminal durably as they happen; a snapshot that
# still holds a stale (earlier) in-memory copy must not roll the durable status or
# external_job_id backwards. Terminal states all rank highest.
_PROVIDER_STATUS_RANK = {
    "prepared": 0,
    "submitted": 1,
    "polling": 2,
    "succeeded": 3,
    "failed": 3,
    "timed_out": 3,
    "cancelled": 3,
}

NODE_LABELS = {
    "ValidateRequest": "校验请求",
    "LoadCaseContext": "加载 Case 上下文",
    "ResolveCreativeIntent": "解析创作意图",
    "TTS": "生成配音",
    "MaterialPackPlanning": "规划素材包",
    "NarrationAlignment": "对齐旁白",
    "NarrationBoundaryPlanning": "规划旁白边界",
    "TimelineWindowPlanning": "编译时间线窗口",
    "WindowQueryPlanning": "规划窗口检索意图",
    "WindowMaterialRetrieval": "检索窗口素材",
    "DeterministicEditingPlanning": "确定性剪辑规划",
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
    running = len([node for node in node_runs if node.status == NodeStatus.running])
    # Node runs are created lazily, so divide by the template's *total* node count,
    # not the count created so far (which would pin progress at ~95% immediately).
    total = max(expected_node_count(run.workflow_template_id), len(node_runs))
    return min(0.95, max(0.05, (completed + 0.5 * running) / max(total, 1)))


def _current_node_label(node_runs: list[NodeRun]) -> str | None:
    running = next((node for node in reversed(node_runs) if node.status == NodeStatus.running), None)
    if running is not None:
        return _node_label(running.node_id)
    latest = next((node for node in reversed(node_runs) if node.status != NodeStatus.pending), None)
    return _node_label(latest.node_id if latest else None)


def _run_title(job: Job, finished_video_title: str | None = None) -> str:
    # Prefer the run's finished-video headline (the generated title) over the persona-
    # label request title / raw script prefix; fall back for in-flight / failed runs.
    if finished_video_title and finished_video_title.strip():
        return finished_video_title.strip()
    if isinstance(job.request, DigitalHumanVideoRequest):
        return job.request.title or job.request.script[:28] or job.id
    return job.id


def _selection_ledger_entry_from_row(row: SelectionLedgerRow) -> SelectionLedgerEntry:
    return SelectionLedgerEntry(
        id=row.id,
        case_id=row.case_id,
        run_id=row.run_id,
        medium=row.medium,
        asset_id=row.asset_id,
        clip_id=row.clip_id,
        slot_phase=row.slot_phase,
        diversity_key=row.diversity_key,
        created_at=row.created_at,
    )


def _clip_embedding_record_from_row(row: ClipEmbeddingIndexRow) -> ClipEmbeddingRecord:
    return ClipEmbeddingRecord(
        clip_embedding_key=row.clip_embedding_key,
        asset_id=row.asset_id,
        asset_revision=row.asset_revision,
        clip_id=row.clip_id,
        source_start=row.source_start,
        source_end=row.source_end,
        source_frames_available=row.source_frames_available,
        index_namespace=row.index_namespace,  # type: ignore[arg-type]
        embedding_scope=row.embedding_scope,  # type: ignore[arg-type]
        embedding_input_type=row.embedding_input_type,  # type: ignore[arg-type]
        embedding_input_ref=row.embedding_input_ref,
        sample_policy=row.sample_policy or {},
        embedding_id=row.embedding_id,
        embedding=_clip_embedding_values(row.embedding),
        provider_profile_id=row.provider_profile_id,
        embedding_model=row.embedding_model,
        embedding_dimension=row.embedding_dimension,
        normalization=row.normalization,
        instruct=row.instruct,
        index_version=row.index_version,
    )


def _clip_embedding_values(value: object | None) -> list[float]:
    if value is None:
        return []
    if isinstance(value, str):
        text_value = value.strip()
        if text_value.startswith("[") and text_value.endswith("]"):
            inner = text_value[1:-1].strip()
            if not inner:
                return []
            return [float(item) for item in inner.split(",")]
    return [float(item) for item in value]  # type: ignore[union-attr]


def _nearest_clip_embeddings_statement(
    *,
    clip_embedding_keys: Sequence[str],
    namespace: str,
    provider_profile_id: str,
    embedding_model: str,
    embedding_dimension: int,
    normalization: str,
    index_version: str,
    min_source_frames_available: int,
    limit: int,
):
    distance = ClipEmbeddingIndexRow.embedding.op("<=>", return_type=Float)(
        bindparam("query_embedding", type_=Vector(1024))
    ).label("distance")
    return (
        select(ClipEmbeddingIndexRow, distance)
        .where(ClipEmbeddingIndexRow.clip_embedding_key.in_(list(clip_embedding_keys)))
        .where(ClipEmbeddingIndexRow.index_namespace == namespace)
        .where(ClipEmbeddingIndexRow.provider_profile_id == provider_profile_id)
        .where(ClipEmbeddingIndexRow.embedding_model == embedding_model)
        .where(ClipEmbeddingIndexRow.embedding_dimension == embedding_dimension)
        .where(ClipEmbeddingIndexRow.normalization == normalization)
        .where(ClipEmbeddingIndexRow.index_version == index_version)
        .where(ClipEmbeddingIndexRow.source_frames_available >= min_source_frames_available)
        .order_by(distance, ClipEmbeddingIndexRow.clip_embedding_key.asc())
        .limit(limit)
    )


def _selection_reservation_from_row(row: SelectionReservationRow) -> SelectionReservationRecord:
    return SelectionReservationRecord(
        id=row.id,
        case_id=row.case_id,
        run_id=row.run_id,
        medium=row.medium,
        asset_id=row.asset_id,
        diversity_key=row.diversity_key,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        committed_at=row.committed_at,
        released_at=row.released_at,
    )


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


def _run_card_from_parts(
    *,
    run: WorkflowRun,
    job: Job,
    node_runs: list[NodeRun],
    has_finished_video: bool,
    finished_video_title: str | None = None,
    preview_url: str | None = None,
) -> RunCard:
    return RunCard(
        run_id=run.id,
        job_id=run.job_id,
        case_id=run.case_id or job.case_id or "",
        status=run.status,
        progress=_run_progress(run, node_runs),
        current_node_label=_current_node_label(node_runs),
        title=_run_title(job, finished_video_title),
        warnings=_run_warnings(node_runs),
        can_resume=has_retryable_active_failure(run, node_runs),
        can_retry=run.status in {RunStatus.failed, RunStatus.cancelled},
        can_publish=run.status == RunStatus.succeeded and has_finished_video,
        preview_url=preview_url,
        started_at=run.started_at,
        updated_at=run.updated_at,
    )


class _ImportRowConflict(Exception):
    """Recoverable per-row import conflict: fail just this row, do not abort the batch."""


class SqlAlchemyProductionRepository(BaseRepository):
    def __init__(self, session_factory: sessionmaker[Session], object_store: ObjectStore | None = None) -> None:
        super().__init__(session_factory)
        self.object_store = object_store or get_object_store()

    def nearest_clip_embeddings(
        self,
        *,
        clip_embedding_keys: Sequence[str],
        query_embedding: Sequence[float],
        namespace: str,
        provider_profile_id: str,
        embedding_model: str,
        embedding_dimension: int,
        normalization: str,
        index_version: str,
        min_source_frames_available: int,
        limit: int,
    ) -> list[tuple[ClipEmbeddingRecord, float]]:
        if not clip_embedding_keys or limit <= 0:
            return []
        statement = _nearest_clip_embeddings_statement(
            clip_embedding_keys=clip_embedding_keys,
            namespace=namespace,
            provider_profile_id=provider_profile_id,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            normalization=normalization,
            index_version=index_version,
            min_source_frames_available=min_source_frames_available,
            limit=limit,
        )
        with self.session_factory() as session:
            # Keep the nearest-neighbor query on the HNSW path even when small
            # filtered candidate sets make a btree scan + explicit sort look cheap.
            session.execute(text("set local enable_sort = off"))
            rows = session.execute(
                statement,
                {"query_embedding": list(query_embedding)},
            ).all()
        return [
            (_clip_embedding_record_from_row(row), float(distance))
            for row, distance in rows
        ]

    def persist_job(self, job: Job) -> None:
        """Persist a standalone Job (e.g. an annotation_batch job with no workflow run)."""
        with self.session_factory() as session:
            session.merge(self._job_row(job))
            session.commit()

    def sync_workflow_snapshot(
        self,
        *,
        job: Job,
        run: WorkflowRun,
        repository: Repository,
    ) -> None:
        for attempt in range(_FINISHED_VIDEO_NUMBER_RETRY_LIMIT):
            try:
                self._sync_workflow_snapshot_once(job=job, run=run, repository=repository)
                return
            except IntegrityError as exc:
                if self._is_selection_reservation_active_slot_conflict(exc):
                    raise NodeExecutionError(
                        ErrorCode.validation_conflict,
                        "Active selection reservation conflict; retry material planning.",
                        retryable=True,
                        details={"constraint": _SELECTION_RESERVATION_ACTIVE_SLOT_CONSTRAINT},
                    ) from exc
                if attempt == _FINISHED_VIDEO_NUMBER_RETRY_LIMIT - 1 or not self._is_finished_video_number_conflict(exc):
                    raise

    def _sync_workflow_snapshot_once(
        self,
        *,
        job: Job,
        run: WorkflowRun,
        repository: Repository,
    ) -> None:
        with self.session_factory() as session:
            durable_run = session.get(WorkflowRunRow, run.id, with_for_update=True)
            cancellation_fence = bool(
                durable_run is not None
                and durable_run.status in _CANCELLATION_FENCE_STATUS_VALUES
            )

            durable_job = session.get(JobRow, job.id)
            job_to_sync = self._preserve_durable_job(
                durable_job,
                job,
                cancellation_fence=cancellation_fence,
            )
            session.merge(self._job_row(job_to_sync))
            session.flush()

            node_runs = repository.node_runs.get(run.id, [])
            blocked_output_ids: set[str] = set()
            node_runs_to_sync: list[NodeRun] = []
            for node_run in node_runs:
                durable_node = session.get(NodeRunRow, node_run.id)
                if (
                    cancellation_fence
                    and node_run.status.value in _COMMITTED_NODE_STATUS_VALUES
                    and (
                        durable_node is None
                        or durable_node.status not in _COMMITTED_NODE_STATUS_VALUES
                    )
                ):
                    blocked_output_ids.update(node_run.output_artifact_ids)
                    continue
                node_runs_to_sync.append(node_run)

            run_artifacts = [
                artifact
                for artifact in repository.artifacts.values()
                if artifact.run_id == run.id
            ]
            for artifact in run_artifacts:
                durable_artifact = session.get(ArtifactRow, artifact.id)
                if (
                    cancellation_fence
                    and durable_artifact is None
                    and (
                        artifact.id in blocked_output_ids
                        or artifact.kind in _DELIVERY_ARTIFACT_KINDS
                    )
                ):
                    record_artifact_commit_skipped_cancelled()
                    continue
                session.merge(artifact_to_row(artifact))
            session.flush()

            run_to_sync = self._preserve_terminal_workflow_run(
                durable_run,
                run,
            )
            run_row = self._workflow_run_row(run_to_sync)
            if durable_run is not None:
                run_row.cancel_mode = durable_run.cancel_mode
                run_row.cancel_requested_at = durable_run.cancel_requested_at
            session.merge(run_row)
            session.flush()

            for node_run in node_runs_to_sync:
                session.merge(self._node_run_row(node_run))
            session.flush()

            provider_invocation_ids = set()
            run_invocations = [
                invocation
                for invocation in repository.provider_invocations.values()
                if invocation.run_id == run.id
            ]
            if run_invocations:
                durable_invocation_progress = self._durable_invocation_progress(session, run.id)
                for invocation in run_invocations:
                    provider_invocation_ids.add(invocation.id)
                    row = self._provider_invocation_row(invocation)
                    self._preserve_durable_invocation_progress(
                        row, durable_invocation_progress.get(invocation.id)
                    )
                    session.merge(row)
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
                    if cancellation_fence and session.get(ScriptVersionRow, script.id) is None:
                        continue
                    session.merge(self._script_version_row(script))
            session.flush()

            finished_video_ids = set()
            blocked_finished_video_ids = set()
            for finished in repository.finished_videos.values():
                if finished.run_id == run.id:
                    existing = session.get(FinishedVideoRow, finished.id)
                    if cancellation_fence and existing is None:
                        blocked_finished_video_ids.add(finished.id)
                        continue
                    owner_user_id = self._finished_video_owner_user_id(
                        session,
                        finished=finished,
                        existing=existing,
                        run=run,
                        job=job,
                    )
                    if existing is None:
                        finished = finished.model_copy(
                            update={"video_number": self._next_finished_video_number(session, finished.case_id)}
                        )
                    elif existing.video_number and existing.video_number != finished.video_number:
                        finished = finished.model_copy(update={"video_number": existing.video_number})
                    elif not existing.video_number:
                        finished = finished.model_copy(
                            update={"video_number": self._next_finished_video_number(session, finished.case_id)}
                        )
                    if owner_user_id and owner_user_id != finished.owner_user_id:
                        finished = finished.model_copy(update={"owner_user_id": owner_user_id})
                    repository.finished_videos[finished.id] = finished
                    finished_video_ids.add(finished.id)
                    session.merge(self._finished_video_row(finished))
                    session.flush()

            for version in repository.video_versions.values():
                if version.finished_video_id in finished_video_ids:
                    if cancellation_fence and session.get(VideoVersionRow, version.id) is None:
                        continue
                    session.merge(self._video_version_row(version))
            session.flush()

            run_finished_video_ids = finished_video_ids | blocked_finished_video_ids
            blocked_publish_package_ids = set()
            for package in repository.publish_packages.values():
                if package.source_finished_video_id in run_finished_video_ids:
                    if cancellation_fence and session.get(PublishPackageRow, package.id) is None:
                        blocked_publish_package_ids.add(package.id)
                        continue
                    session.merge(self._publish_package_row(package))
            session.flush()

            for event in repository.outbox.values():
                if event.aggregate_type in {"run", "workflow_run"} and event.aggregate_id == run.id:
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    if (
                        cancellation_fence
                        and session.get(OutboxEventRow, event.id) is None
                        and (
                            event.topic in _CANCELLATION_BLOCKED_OUTBOX_TOPICS
                            or payload.get("status")
                            in {
                                RunStatus.succeeded.value,
                                NodeStatus.succeeded.value,
                                NodeStatus.degraded.value,
                            }
                            or payload.get("finished_video_id")
                            in blocked_finished_video_ids
                            or payload.get("publish_package_id")
                            in blocked_publish_package_ids
                        )
                    ):
                        continue
                    session.merge(self._outbox_event_row(event))
            for event in repository.yield_events.values():
                if getattr(event, "run_id", None) == run.id:
                    if (
                        cancellation_fence
                        and session.get(YieldFunnelEventRow, event.id) is None
                    ):
                        continue
                    owner_user_id = resolve_event_owner(
                        session,
                        run_id=getattr(event, "run_id", None),
                        job_id=getattr(event, "job_id", None),
                        finished_video_id=getattr(event, "finished_video_id", None),
                    )
                    session.merge(
                        self._yield_funnel_event_row(event, run.case_id, owner_user_id)
                    )
            for entry in repository.failures.values():
                if getattr(entry, "run_id", None) == run.id:
                    session.merge(
                        self._failure_taxonomy_row(
                            entry, repository._failure_dedupe_keys
                        )
                    )
            for entry in repository.selection_ledger.values():
                if entry.run_id == run.id:
                    if cancellation_fence and session.get(SelectionLedgerRow, entry.id) is None:
                        continue
                    session.merge(self._selection_ledger_row(entry))
            self._expire_stale_selection_reservations(session)
            for reservation in repository.selection_reservations.values():
                if reservation.run_id == run.id:
                    if cancellation_fence:
                        durable_reservation = session.get(
                            SelectionReservationRow,
                            reservation.id,
                        )
                        if durable_reservation is None or reservation.status not in {
                            "released",
                            "expired",
                        }:
                            continue
                    session.merge(self._selection_reservation_row(reservation))
            session.commit()

    @staticmethod
    def _is_finished_video_number_conflict(exc: IntegrityError) -> bool:
        original = getattr(exc, "orig", None)
        diagnostic = getattr(original, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)
        if constraint_name == _FINISHED_VIDEO_NUMBER_CONSTRAINT:
            return True
        message = str(original or exc)
        return _FINISHED_VIDEO_NUMBER_CONSTRAINT in message or (
            "finished_videos.case_id" in message and "finished_videos.video_number" in message
        )

    @staticmethod
    def _is_selection_reservation_active_slot_conflict(exc: IntegrityError) -> bool:
        original = getattr(exc, "orig", None)
        diagnostic = getattr(original, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)
        if constraint_name == _SELECTION_RESERVATION_ACTIVE_SLOT_CONSTRAINT:
            return True
        message = str(original or exc)
        return _SELECTION_RESERVATION_ACTIVE_SLOT_CONSTRAINT in message or (
            "selection_reservations.case_id" in message
            and "selection_reservations.medium" in message
            and "selection_reservations.asset_id" in message
        )

    @staticmethod
    def _preserve_durable_job(
        existing: JobRow | None,
        incoming: Job,
        *,
        cancellation_fence: bool,
    ) -> Job:
        if existing is None:
            return incoming
        if cancellation_fence and incoming.status != JobStatus.cancelled:
            return SqlAlchemyProductionRepository._job_with_durable_state(existing, incoming)
        return incoming

    @staticmethod
    def _job_with_durable_state(existing: JobRow, incoming: Job) -> Job:
        """Keep the locked row's lifecycle state without reparsing its request payload."""
        return incoming.model_copy(
            update={
                "status": JobStatus(existing.status),
                "active_run_id": existing.active_run_id,
                "latest_finished_video_id": existing.latest_finished_video_id,
                "schema_version": existing.schema_version,
                "created_at": existing.created_at,
                "updated_at": existing.updated_at,
            }
        )

    @staticmethod
    def _preserve_terminal_workflow_run(
        existing: WorkflowRunRow | None,
        incoming: WorkflowRun,
    ) -> WorkflowRun:
        if existing is None:
            return incoming
        if existing.status == RunStatus.cancelling.value:
            if incoming.status == RunStatus.cancelled:
                return incoming
            return incoming.model_copy(
                update={
                    "status": RunStatus.cancelling,
                    "finished_at": existing.finished_at,
                    "public_report_artifact_id": existing.public_report_artifact_id,
                    "debug_report_artifact_id": existing.debug_report_artifact_id,
                    "updated_at": existing.updated_at or incoming.updated_at,
                }
            )
        if existing.status not in _TERMINAL_RUN_STATUS_VALUES:
            return incoming
        if incoming.status.value == existing.status:
            return incoming
        try:
            existing_status = RunStatus(existing.status)
        except ValueError:
            return incoming
        return incoming.model_copy(
            update={
                "status": existing_status,
                "finished_at": existing.finished_at or incoming.finished_at,
                "public_report_artifact_id": existing.public_report_artifact_id
                or incoming.public_report_artifact_id,
                "debug_report_artifact_id": existing.debug_report_artifact_id
                or incoming.debug_report_artifact_id,
                "updated_at": existing.updated_at or incoming.updated_at,
            }
        )

    @staticmethod
    def _expire_stale_selection_reservations(session: Session) -> None:
        execute = getattr(session, "execute", None)
        if not callable(execute):
            return
        now = utcnow()
        execute(
            update(SelectionReservationRow)
            .where(SelectionReservationRow.status == "reserved")
            .where(SelectionReservationRow.expires_at <= now)
            .values(status="expired", released_at=now)
        )

    @staticmethod
    def _hydrate_selection_reservations(
        session: Session,
        repository: Repository,
        *,
        case_id: str,
        run_id: str,
    ) -> None:
        active_statement = (
            select(SelectionReservationRow)
            .where(SelectionReservationRow.case_id == case_id)
            .where(SelectionReservationRow.status == "reserved")
            .where(SelectionReservationRow.expires_at > utcnow())
            .order_by(SelectionReservationRow.created_at.desc())
        )
        current_run_statement = (
            select(SelectionReservationRow)
            .where(SelectionReservationRow.run_id == run_id)
            .where(SelectionReservationRow.status.in_(("reserved", "committed")))
        )
        for statement in (active_statement, current_run_statement):
            for row in session.scalars(statement):
                reservation = _selection_reservation_from_row(row)
                repository.selection_reservations[reservation.id] = reservation

    def reserve_selection_candidates(
        self,
        repository: Repository,
        *,
        case_id: str,
        run_id: str,
        asset_ids_by_medium: Mapping[str, Sequence[str]],
        diversity_keys_by_medium: Mapping[str, Mapping[str, str | None]],
    ) -> dict[str, list[SelectionReservationRecord]]:
        """Atomically acquire a MaterialPack candidate batch in Postgres.

        Hydration is necessarily a snapshot: two runs can both plan against an
        apparently-free slot. Persisting the leases here moves the partial unique
        constraint into MaterialPackPlanning's declared retry boundary. On a race,
        restore the caller's in-memory leases, refresh the winner from SQL, and raise
        a retryable conflict so the next attempt replans without that asset.
        """
        previous_for_run = {
            reservation_id: reservation
            for reservation_id, reservation in repository.selection_reservations.items()
            if reservation.run_id == run_id
        }
        owned_by_medium: dict[str, list[SelectionReservationRecord]] = {}
        for medium, asset_ids in asset_ids_by_medium.items():
            owned_by_medium[medium] = repository.reserve_selections(
                case_id=case_id,
                run_id=run_id,
                medium=medium,
                asset_ids=asset_ids,
                diversity_keys=dict(diversity_keys_by_medium.get(medium, {})),
            )
        owned = [reservation for values in owned_by_medium.values() for reservation in values]
        if not owned:
            return owned_by_medium
        try:
            with self.session_factory() as session:
                self._expire_stale_selection_reservations(session)
                for reservation in owned:
                    session.merge(self._selection_reservation_row(reservation))
                session.commit()
        except IntegrityError as exc:
            for reservation_id, reservation in list(repository.selection_reservations.items()):
                if reservation.run_id == run_id:
                    del repository.selection_reservations[reservation_id]
            repository.selection_reservations.update(previous_for_run)
            if not self._is_selection_reservation_active_slot_conflict(exc):
                raise
            with self.session_factory() as session:
                self._hydrate_selection_reservations(
                    session,
                    repository,
                    case_id=case_id,
                    run_id=run_id,
                )
            raise NodeExecutionError(
                ErrorCode.validation_conflict,
                "Active selection reservation conflict; retry material planning.",
                retryable=True,
                details={"constraint": _SELECTION_RESERVATION_ACTIVE_SLOT_CONSTRAINT},
            ) from exc
        return owned_by_medium

    def release_run_selection_reservations(self, run_id: str) -> int:
        """Persist cancellation cleanup even when the API runtime cache is stale.

        Temporal activities create reservations in independently hydrated repositories.
        The API process that later receives a cancel request may therefore know the run
        but not those newly-created reservation objects. Releasing directly by run id
        keeps the durable active-slot constraint from blocking the next run.
        """

        now = utcnow()
        with self.session_factory() as session:
            result = session.execute(
                update(SelectionReservationRow)
                .where(SelectionReservationRow.run_id == run_id)
                .where(SelectionReservationRow.status == "reserved")
                .values(status="released", released_at=now)
            )
            session.commit()
            return max(0, int(result.rowcount or 0))

    def case_run_cards(
        self,
        *,
        case_id: str,
        request_id: str,
        limit: int = 50,
        owner_user_id: str | None = None,
    ) -> PageResponse[RunCard] | None:
        with self.session_factory() as session:
            if session.get(CaseRow, case_id) is None:
                return None
            statement = (
                select(WorkflowRunRow)
                .where(WorkflowRunRow.case_id == case_id)
                .order_by(WorkflowRunRow.updated_at.desc())
                .limit(limit)
            )
            if owner_user_id is not None:
                # Creator-based isolation (spec §3): join the owning job and filter by
                # its created_by (admin passes owner_user_id=None and sees all).
                statement = statement.join(
                    JobRow, JobRow.id == WorkflowRunRow.job_id
                ).where(JobRow.created_by == owner_user_id)
            run_rows = list(session.scalars(statement))
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
                fv_row = session.scalar(
                    select(FinishedVideoRow).where(FinishedVideoRow.run_id == run.id).limit(1)
                )
                items.append(
                    _run_card_from_parts(
                        run=run,
                        job=job_row_to_contract(job_row),
                        node_runs=node_runs,
                        has_finished_video=fv_row is not None,
                        finished_video_title=fv_row.title if fv_row is not None else None,
                        preview_url=self._signed_run_thumbnail(fv_row),
                    )
                )
            return PageResponse(items=items, total_hint=len(items), request_id=request_id)

    def run_overview(
        self,
        *,
        request_id: str,
        limit: int = 50,
        cursor: str | None = None,
        status: RunStatus | None = None,
        run_ids: Sequence[str] | None = None,
        owner_user_id: str | None = None,
    ) -> RunOverviewResponse:
        limit = max(1, min(100, int(limit or 50)))
        try:
            offset = max(0, int(cursor or "0"))
        except ValueError:
            offset = 0
        with self.session_factory() as session:
            base_statement = select(WorkflowRunRow)
            count_statement = select(WorkflowRunRow.status, func.count()).group_by(WorkflowRunRow.status)
            if status is not None:
                base_statement = base_statement.where(WorkflowRunRow.status == status.value)
                count_statement = count_statement.where(WorkflowRunRow.status == status.value)
            if run_ids:
                ids = list(dict.fromkeys(run_ids))
                base_statement = base_statement.where(WorkflowRunRow.id.in_(ids))
                count_statement = count_statement.where(WorkflowRunRow.id.in_(ids))
            if owner_user_id is not None:
                base_statement = base_statement.join(
                    JobRow, JobRow.id == WorkflowRunRow.job_id
                ).where(JobRow.created_by == owner_user_id)
                count_statement = count_statement.join(
                    JobRow, JobRow.id == WorkflowRunRow.job_id
                ).where(JobRow.created_by == owner_user_id)
            run_rows = list(
                session.scalars(
                    base_statement.order_by(
                        WorkflowRunRow.updated_at.desc(),
                        WorkflowRunRow.created_at.desc(),
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            next_cursor = str(offset + limit) if len(run_rows) > limit else None
            run_rows = run_rows[:limit]
            status_counts = {str(key): int(value) for key, value in session.execute(count_statement)}

            # The visible-run window is expressed as a filtered subquery, never a
            # materialized id list: a large owner/status window could otherwise blow
            # past Postgres' 32767 bind-parameter cap when spliced into an IN clause.
            def _visible_run_ids():
                stmt = select(WorkflowRunRow.id)
                if status is not None:
                    stmt = stmt.where(WorkflowRunRow.status == status.value)
                if run_ids:
                    stmt = stmt.where(WorkflowRunRow.id.in_(list(dict.fromkeys(run_ids))))
                if owner_user_id is not None:
                    stmt = stmt.join(JobRow, JobRow.id == WorkflowRunRow.job_id).where(
                        JobRow.created_by == owner_user_id
                    )
                return stmt

            failure_statement = (
                select(FailureTaxonomyRow.error_code, func.count())
                .where(FailureTaxonomyRow.run_id.in_(_visible_run_ids()))
                .group_by(FailureTaxonomyRow.error_code)
            )
            failure_counts = {
                str(code or "unknown"): int(value)
                for code, value in session.execute(failure_statement)
            }

            # Count degradation codes inside the DB by expanding each node's
            # ``degradations`` JSONB array with a lateral ``jsonb_array_elements``.
            # Elements are either objects ({code|degradation_code}) or bare strings;
            # the CASE mirrors the former Python normalization (empty/missing -> skip).
            deg_elements = func.jsonb_array_elements(NodeRunRow.degradations).table_valued(
                "value", name="degradation"
            )
            deg_value = deg_elements.c.value
            degradation_code = case(
                (
                    func.jsonb_typeof(deg_value) == "object",
                    func.coalesce(
                        func.nullif(deg_value.op("->>")("code"), ""),
                        func.nullif(deg_value.op("->>")("degradation_code"), ""),
                    ),
                ),
                (
                    func.jsonb_typeof(deg_value) == "string",
                    func.nullif(deg_value.op("#>>")(cast([], ARRAY(Text))), ""),
                ),
                else_=None,
            )
            degradation_statement = (
                select(degradation_code.label("code"), func.count())
                .select_from(NodeRunRow)
                .join(deg_elements, true())
                .where(NodeRunRow.run_id.in_(_visible_run_ids()))
                .where(degradation_code.is_not(None))
                .group_by(degradation_code)
            )
            degradation_counts = {
                str(code): int(value)
                for code, value in session.execute(degradation_statement)
            }

            # Batch-prefetch the page's rows (<= 100 runs) in three IN queries rather
            # than issuing per-run JobRow/NodeRunRow/FinishedVideoRow lookups (N+1).
            page_run_ids = [run_row.id for run_row in run_rows]
            jobs_by_id: dict[str, JobRow] = {}
            node_runs_by_run: dict[str, list[NodeRun]] = {}
            finished_by_run: dict[str, FinishedVideoRow] = {}
            if page_run_ids:
                page_job_ids = [run_row.job_id for run_row in run_rows]
                jobs_by_id = {
                    job_row.id: job_row
                    for job_row in session.scalars(
                        select(JobRow).where(JobRow.id.in_(page_job_ids))
                    )
                }
                for node_row in session.scalars(
                    select(NodeRunRow)
                    .where(NodeRunRow.run_id.in_(page_run_ids))
                    .order_by(NodeRunRow.run_id.asc(), NodeRunRow.created_at.asc())
                ):
                    node_runs_by_run.setdefault(node_row.run_id, []).append(
                        node_run_row_to_contract(node_row)
                    )
                for fv_row in session.scalars(
                    select(FinishedVideoRow)
                    .where(FinishedVideoRow.run_id.in_(page_run_ids))
                    .order_by(FinishedVideoRow.created_at.asc(), FinishedVideoRow.id.asc())
                ):
                    finished_by_run.setdefault(fv_row.run_id, fv_row)

            items: list[RunCard] = []
            for run_row in run_rows:
                job_row = jobs_by_id.get(run_row.job_id)
                if job_row is None:
                    continue
                run = workflow_run_row_to_contract(run_row)
                fv_row = finished_by_run.get(run.id)
                items.append(
                    _run_card_from_parts(
                        run=run,
                        job=job_row_to_contract(job_row),
                        node_runs=node_runs_by_run.get(run.id, []),
                        has_finished_video=fv_row is not None,
                        finished_video_title=fv_row.title if fv_row is not None else None,
                        preview_url=self._signed_run_thumbnail(fv_row),
                    )
                )
            total_hint = sum(status_counts.values())
            return RunOverviewResponse(
                items=items,
                next_cursor=next_cursor,
                total_hint=total_hint,
                status_counts=status_counts,
                failure_code_counts=failure_counts,
                degradation_code_counts=degradation_counts,
                request_id=request_id,
            )

    def batch_feasibility(
        self,
        *,
        case_id: str,
        estimated_audio_duration_sec: float,
        request_id: str,
    ) -> BatchFeasibilityResponse | None:
        audio_duration = max(0.0, float(estimated_audio_duration_sec or 0.0))
        with self.session_factory() as session:
            if session.get(CaseRow, case_id) is None:
                return None
            media_rows = list(
                session.scalars(
                    select(MediaAssetRow)
                    .where(or_(MediaAssetRow.case_id == case_id, MediaAssetRow.case_id.is_(None)))
                    .where(MediaAssetRow.usable.is_(True))
                )
            )
        video_rows = [row for row in media_rows if row.kind == "video"]
        annotated_rows = [row for row in video_rows if row.annotation_status == "annotated"]
        portrait_rows = [
            row
            for row in annotated_rows
            if any(tag in {"portrait", "digital_human"} for tag in (row.tags or []))
        ]
        if not portrait_rows:
            portrait_rows = annotated_rows
        portrait_duration = sum(float(row.duration_sec or 0.0) for row in portrait_rows)
        clean_broll_count = len(annotated_rows)
        estimated_windows = max(1, int(math.ceil(audio_duration / 4.0))) if audio_duration > 0 else 1
        notes: list[str] = []
        portrait_ok = portrait_duration >= audio_duration if audio_duration > 0 else portrait_duration > 0
        broll_ok = clean_broll_count >= estimated_windows
        if not portrait_ok:
            notes.append("portrait_duration_insufficient")
        if not broll_ok:
            notes.append("clean_broll_candidates_insufficient")
        if not annotated_rows:
            notes.append("no_annotated_video_material")
        return BatchFeasibilityResponse(
            case_id=case_id,
            estimated_audio_duration_sec=audio_duration,
            portrait_duration_sec=portrait_duration,
            clean_broll_candidate_count=clean_broll_count,
            estimated_broll_window_count=estimated_windows,
            portrait_ok=portrait_ok,
            broll_ok=broll_ok,
            notes=notes,
            request_id=request_id,
        )

    def admit_case_runs(
        self,
        *,
        case_id: str,
        max_inflight: int,
    ) -> dict[str, object]:
        """FIFO-select admitted case runs up to the configured in-flight cap.

        Runs stay durable ``admitted`` until the Temporal workflow start succeeds.
        This keeps admission retryable if the worker crashes after this transaction
        but before ``Client.start_workflow`` returns.
        """
        max_inflight = max(1, int(max_inflight or 1))
        admitted: list[tuple[Job, WorkflowRun]] = []
        with self.session_factory() as session:
            # Serialize the read-running-count + select-admitted decision per case:
            # the Temporal admit activity runs at-least-once, so two concurrent
            # invocations for the same case could each see the same running count
            # and over-admit past ``max_inflight``. A transaction-scoped advisory
            # lock (released on commit) makes the whole selection atomic per case.
            session.execute(
                text("select pg_advisory_xact_lock(hashtext(:cid))"),
                {"cid": case_id},
            )
            running_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(WorkflowRunRow)
                    .where(WorkflowRunRow.case_id == case_id)
                    .where(WorkflowRunRow.status == RunStatus.running.value)
                )
                or 0
            )
            slots = max(0, max_inflight - running_count)
            if slots > 0:
                queued_rows = list(
                    session.scalars(
                        select(WorkflowRunRow)
                        .where(WorkflowRunRow.case_id == case_id)
                        .where(WorkflowRunRow.status == RunStatus.admitted.value)
                        .order_by(WorkflowRunRow.created_at.asc(), WorkflowRunRow.id.asc())
                        .limit(slots)
                        .with_for_update(skip_locked=True)
                    )
                )
                for run_row in queued_rows:
                    job_row = session.get(JobRow, run_row.job_id, with_for_update=True)
                    if job_row is None:
                        continue
                    admitted.append(
                        (job_row_to_contract(job_row), workflow_run_row_to_contract(run_row))
                    )
            queued_remaining = int(
                session.scalar(
                    select(func.count())
                    .select_from(WorkflowRunRow)
                    .where(WorkflowRunRow.case_id == case_id)
                    .where(WorkflowRunRow.status == RunStatus.admitted.value)
                )
                or 0
            )
            active_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(WorkflowRunRow)
                    .where(WorkflowRunRow.case_id == case_id)
                    .where(WorkflowRunRow.status == RunStatus.running.value)
                )
                or 0
            )
            session.commit()
        return {
            "admitted": admitted,
            "active_count": active_count,
            "queued_remaining": queued_remaining,
        }

    def case_ids_with_admitted_runs(self, *, limit: int = 100) -> list[str]:
        limit = max(1, min(500, int(limit or 100)))
        with self.session_factory() as session:
            rows = session.scalars(
                select(WorkflowRunRow.case_id)
                .where(WorkflowRunRow.status == RunStatus.admitted.value)
                .where(WorkflowRunRow.case_id.is_not(None))
                .distinct()
                .order_by(WorkflowRunRow.case_id.asc())
                .limit(limit)
            )
            return [str(case_id) for case_id in rows if case_id]

    def mark_run_started(self, run_id: str) -> None:
        now = utcnow()
        with self.session_factory() as session:
            run_row = session.get(WorkflowRunRow, run_id, with_for_update=True)
            if run_row is None or run_row.status != RunStatus.admitted.value:
                return
            job_row = session.get(JobRow, run_row.job_id, with_for_update=True)
            run_row.status = RunStatus.running.value
            run_row.started_at = run_row.started_at or now
            run_row.updated_at = now
            if job_row is not None:
                job_row.status = JobStatus.running.value
                job_row.active_run_id = run_row.id
                job_row.updated_at = now
            session.commit()

    def request_run_cancellation(self, run_id: str, *, force: bool) -> WorkflowRun:
        """Persist the cancellation fence before signalling Temporal."""
        now = utcnow()
        with self.session_factory() as session:
            run_row = session.get(WorkflowRunRow, run_id, with_for_update=True)
            if run_row is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, f"Run {run_id} is missing.")
            if run_row.status in _TERMINAL_RUN_STATUS_VALUES:
                return workflow_run_row_to_contract(run_row)

            run_row.cancel_mode = (
                "force" if force or run_row.cancel_mode == "force" else "graceful"
            )
            run_row.cancel_requested_at = run_row.cancel_requested_at or now
            run_row.updated_at = now
            if run_row.status == RunStatus.running.value:
                run_row.status = RunStatus.cancelling.value
            elif run_row.status != RunStatus.cancelling.value:
                run_row.status = RunStatus.cancelled.value
                run_row.finished_at = now
                job_row = session.get(JobRow, run_row.job_id, with_for_update=True)
                if job_row is not None and job_row.status not in {
                    JobStatus.succeeded.value,
                    JobStatus.failed.value,
                    JobStatus.cancelled.value,
                    JobStatus.archived.value,
                }:
                    job_row.status = JobStatus.cancelled.value
                    job_row.updated_at = now
            session.commit()
            return workflow_run_row_to_contract(run_row)

    def run_cancel_mode(self, run_id: str) -> str:
        return self.requested_run_cancel_mode(run_id) or "graceful"

    def requested_run_cancel_mode(self, run_id: str) -> str | None:
        with self.session_factory() as session:
            row = session.execute(
                select(WorkflowRunRow.status, WorkflowRunRow.cancel_mode).where(
                    WorkflowRunRow.id == run_id
                )
            ).one_or_none()
        if row is None or row.status not in _CANCELLATION_FENCE_STATUS_VALUES:
            return None
        if row.cancel_mode in {"graceful", "force"}:
            return row.cancel_mode
        return "graceful"

    def run_ids_with_cancelling(self, *, limit: int = 100) -> list[str]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(WorkflowRunRow.id)
                    .where(WorkflowRunRow.status == RunStatus.cancelling.value)
                    .order_by(WorkflowRunRow.updated_at.asc())
                    .limit(max(1, min(500, limit)))
                )
            )

    def _signed_run_thumbnail(self, fv_row) -> str | None:
        """Signed https URL for the Outputs card thumbnail. None when there is no
        finished video or no uri can be signed.

        Preference order is smallest-first: the WebP cover thumbnail (~30 KB), then
        the full cover PNG (~2.3 MB, for rows predating the thumbnail), then the
        finished video itself (for rows with no cover at all). The signed URL is
        cached by the object store, so the same card gets the same URL on every
        poll and the browser stops re-downloading it (issue #206).
        """
        if fv_row is None:
            return None
        for art in (fv_row.cover_thumb_artifact, fv_row.cover_artifact, fv_row.video_artifact):
            uri = art.get("uri") if isinstance(art, dict) else None
            if uri and uri.startswith(("s3://", "local://")):
                try:
                    return self.object_store.signed_url(uri).url
                except Exception:
                    continue
        return None

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
            for profile_row in session.scalars(select(ProviderProfileRow)):
                profile = provider_profile_row_to_contract(profile_row)
                repository.provider_profiles[profile.id] = profile
            for voice_row in session.scalars(select(VoiceProfileRow)):
                voice = voice_row_to_contract(voice_row)
                repository.voices[voice.id] = voice

            job = job_row_to_contract(job_row)
            run = workflow_run_row_to_contract(run_row)
            repository.jobs[job.id] = job
            repository.runs[run.id] = run
            creative_intent_ref = getattr(job.request, "creative_intent_ref", None)
            if creative_intent_ref is not None:
                creative_intent_row = session.get(ArtifactRow, creative_intent_ref.artifact_id)
                if creative_intent_row is not None:
                    creative_intent = artifact_row_to_contract(creative_intent_row)
                    repository.artifacts[creative_intent.id] = creative_intent
            # Hydrate the adopted ScriptVersion into the run-scoped runtime repo so the
            # adopted-script provenance survives under the Temporal runtime too. Each
            # run_node activity builds a FRESH Repository, so unless we load it here the
            # export node mints a fresh ScriptVersion and overwrites adopted_from_draft_id.
            adopted_script_version_id = getattr(job.request, "script_version_id", None)
            if adopted_script_version_id and adopted_script_version_id not in repository.scripts:
                script_row = session.get(ScriptVersionRow, adopted_script_version_id)
                if script_row is not None:
                    adopted_script = script_version_row_to_contract(script_row)
                    repository.scripts[adopted_script.id] = adopted_script
            if run.case_id:
                media_statement = select(MediaAssetRow).where(
                    or_(MediaAssetRow.case_id == run.case_id, MediaAssetRow.case_id.is_(None))
                )
                media_rows = list(session.scalars(media_statement))
                rows_by_asset_id = {row.id: row for row in media_rows}
                for row in media_rows:
                    asset = media_asset_row_to_contract(row)
                    repository.media_assets[asset.id] = asset
                # Hydrate the latest annotation per asset so material planning
                # (b-roll matching reads repo.annotation_v4_for_asset ->
                # self.annotations) sees the real AnnotationV4 in the WORKER
                # process. Only API services populate the in-memory annotations
                # dict otherwise, so without this the worker matches against zero
                # annotations and b-roll always soft-degrades. object_store=None
                # skips evidence-image signing (matching never needs the signed
                # URLs). Batched into ONE ``IN`` query ordered by
                # (asset_id, updated_at desc) instead of one query per asset — the
                # media pool spans this case PLUS the global shared pool
                # (case_id IS NULL), so a per-asset query was an N+1 over the whole
                # library. ``setdefault`` keeps the first row seen per asset (its
                # latest annotation).
                asset_ids = list(rows_by_asset_id)
                if asset_ids:
                    latest_ann_by_asset: dict[str, AnnotationRow] = {}
                    annotation_statement = (
                        select(AnnotationRow)
                        .where(AnnotationRow.asset_id.in_(asset_ids))
                        .order_by(AnnotationRow.asset_id, AnnotationRow.updated_at.desc())
                    )
                    for ann_row in session.scalars(annotation_statement):
                        latest_ann_by_asset.setdefault(ann_row.asset_id, ann_row)
                    for asset_id, ann_row in latest_ann_by_asset.items():
                        repository.annotations[asset_id] = annotation_row_to_editor(
                            ann_row, rows_by_asset_id[asset_id], object_store=None
                        )
                    # Batch-load source artifacts with a single ``IN`` query
                    # instead of one ``session.get`` per asset.
                    source_artifact_ids = {
                        row.source_artifact_id
                        for row in media_rows
                        if row.source_artifact_id
                        and row.source_artifact_id not in repository.artifacts
                    }
                    if source_artifact_ids:
                        for artifact_row in session.scalars(
                            select(ArtifactRow).where(ArtifactRow.id.in_(source_artifact_ids))
                        ):
                            contract = artifact_row_to_contract(artifact_row)
                            repository.artifacts[contract.id] = contract
                    embedding_statement = select(ClipEmbeddingIndexRow).where(
                        ClipEmbeddingIndexRow.asset_id.in_(asset_ids)
                    )
                    for embedding_row in session.scalars(embedding_statement):
                        record = _clip_embedding_record_from_row(embedding_row)
                        repository.clip_embedding_index[record.clip_embedding_key] = record
                for video_row in session.scalars(
                    select(FinishedVideoRow).where(FinishedVideoRow.case_id == run.case_id)
                ):
                    repository.finished_videos[video_row.id] = finished_video_row_to_contract(video_row)
                ledger_statement = (
                    select(SelectionLedgerRow)
                    .where(SelectionLedgerRow.case_id == run.case_id)
                    .order_by(SelectionLedgerRow.created_at.desc())
                    .limit(100)
                )
                for ledger_row in session.scalars(ledger_statement):
                    entry = _selection_ledger_entry_from_row(ledger_row)
                    repository.selection_ledger[entry.id] = entry
                self._hydrate_selection_reservations(
                    session,
                    repository,
                    case_id=run.case_id,
                    run_id=run.id,
                )
            run_ids = {run_id}
            if run.resume_from_run_id:
                source_row = session.get(WorkflowRunRow, run.resume_from_run_id)
                if source_row is not None:
                    source_run = workflow_run_row_to_contract(source_row)
                    repository.runs[source_run.id] = source_run
                    run_ids.add(source_run.id)
            # Node order is part of resume state: when several committed outputs share an
            # artifact kind, _state_from_persisted_artifacts applies last-write-wins and
            # the winner feeds the next node's input manifest. (created_at, id) is a total
            # order, so activity retries derive the same provider-call idempotency key.
            node_runs = [
                node_run_row_to_contract(row)
                for row in session.scalars(
                    select(NodeRunRow)
                    .where(NodeRunRow.run_id.in_(run_ids))
                    .order_by(NodeRunRow.created_at.asc(), NodeRunRow.id.asc())
                )
            ]
            repository.node_runs[run_id] = [node for node in node_runs if node.run_id == run_id]
            if run.resume_from_run_id:
                repository.node_runs[run.resume_from_run_id] = [
                    node for node in node_runs if node.run_id == run.resume_from_run_id
                ]
            # A resumed node run keeps the original artifact ids instead of copying
            # their rows to the new run. On a chained resume, the immediate source's
            # skipped node runs can therefore reference artifacts owned by an older
            # ancestor run. Hydrate by actual node references as well as run ownership
            # so apply_reuse_plan sees the same artifact set that admission validated.
            referenced_artifact_ids = {
                artifact_id
                for node_run in node_runs
                if node_run.status
                in {NodeStatus.succeeded, NodeStatus.degraded, NodeStatus.skipped}
                for artifact_id in node_run.output_artifact_ids
            }
            if referenced_artifact_ids:
                for artifact in session.scalars(
                    select(ArtifactRow)
                    .where(ArtifactRow.id.in_(referenced_artifact_ids))
                    .order_by(ArtifactRow.created_at.asc(), ArtifactRow.id.asc())
                ):
                    contract = artifact_row_to_contract(artifact)
                    repository.artifacts[contract.id] = contract

    def hydrate_adopted_script(
        self, repository: Repository, script_version_id: str
    ) -> ScriptVersion | None:
        """Load a previously adopted ScriptVersion into the in-memory runtime repo.

        Called when a DigitalHumanVideo job/run is created with an explicit
        ``script_version_id`` so the adopted ScriptVersion (with its
        ``adopted_from_draft_id`` provenance) is preserved through the run snapshot
        instead of being overwritten by a freshly fabricated row. Returns the
        contract if found, otherwise ``None``.
        """
        with self.session_factory() as session:
            row = session.get(ScriptVersionRow, script_version_id)
            if row is None:
                return None
            script = script_version_row_to_contract(row)
            repository.scripts[script.id] = script
            return script

    def job_owner_user_id(self, job_id: str) -> str | None:
        """Creator-based isolation (spec §3): owner of a job = ``job.created_by``.
        ``None`` when the job is unknown or unowned."""
        with self.session_factory() as session:
            job = session.get(JobRow, job_id)
            return job.created_by if job is not None else None

    def run_owner_user_id(self, run_id: str) -> str | None:
        """Owner of a run = its job's ``created_by``. ``None`` when unknown/unowned."""
        with self.session_factory() as session:
            run = session.get(WorkflowRunRow, run_id)
            if run is None:
                return None
            job = session.get(JobRow, run.job_id)
            return job.created_by if job is not None else None

    def finished_video_owner_user_id(self, finished_video_id: str) -> str | None:
        """Owner of a finished video = its denormalized ``owner_user_id``. ``None``
        when unknown/unowned."""
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            return finished.owner_user_id if finished is not None else None

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
            referenced_artifact_ids = {
                artifact_id for node_run in node_runs for artifact_id in node_run.output_artifact_ids
            }
            artifact_filter = ArtifactRow.run_id == run_id
            if referenced_artifact_ids:
                artifact_filter = or_(artifact_filter, ArtifactRow.id.in_(referenced_artifact_ids))
            artifact_rows = list(
                session.scalars(
                    select(ArtifactRow)
                    .where(artifact_filter)
                    .order_by(ArtifactRow.created_at.asc())
                )
            )
            artifacts = [artifact_ref_from_row(row) for row in artifact_rows]
            payloads = {row.id: row.payload for row in artifact_rows if row.payload is not None}
            job_row = session.get(JobRow, run.job_id)
            config = (
                build_run_config_summary(run_id, job_row_to_contract(job_row))
                if job_row is not None
                else None
            )
            return RunDetailResponse(
                run=workflow_run_row_to_contract(run),
                node_runs=node_runs,
                artifacts=artifacts,
                artifact_payloads=payloads,
                config=config,
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

    def _next_finished_video_number(self, session: Session, case_id: str) -> str:
        return next_finished_video_number(
            session.scalars(select(FinishedVideoRow.video_number).where(FinishedVideoRow.case_id == case_id))
        )

    @staticmethod
    def _finished_video_owner_user_id(
        session: Session,
        *,
        finished: FinishedVideo,
        existing: FinishedVideoRow | None,
        run: WorkflowRun,
        job: Job,
    ) -> str | None:
        if finished.owner_user_id:
            return finished.owner_user_id
        if existing is not None and existing.owner_user_id:
            return existing.owner_user_id
        if finished.run_id == run.id:
            if run.requested_by:
                return run.requested_by
            if job.created_by:
                return job.created_by
        if finished.run_id:
            return resolve_event_owner(
                session,
                run_id=finished.run_id,
                job_id=None,
                finished_video_id=None,
            )
        return None

    def list_finished_videos(
        self, *, case_id: str, limit: int = 50, owner_user_id: str | None = None
    ) -> list[FinishedVideo]:
        with self.session_factory() as session:
            statement = (
                select(FinishedVideoRow)
                .where(FinishedVideoRow.case_id == case_id)
                .order_by(FinishedVideoRow.updated_at.desc())
                .limit(limit)
            )
            if owner_user_id is not None:
                # Creator-based isolation (spec §3): only this owner's finished videos
                # (admin passes owner_user_id=None and sees all, incl. unowned rows).
                statement = statement.where(FinishedVideoRow.owner_user_id == owner_user_id)
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

    def delete_finished_video(self, finished_video_id: str) -> bool:
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                return False
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
            return True

    def create_editor_handoff(
        self, finished_video_id: str, payload: CreateEditorHandoffRequest
    ) -> EditorHandoffPackageArtifact:
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, "Finished video is missing.")
            handoff = EditorHandoffBuilder(self.object_store).build(
                EditorHandoffInput(
                    finished_video_id=finished_video_id,
                    package_format=payload.format,
                    assets=self._handoff_assets(session, finished),
                    effects=self._handoff_effects(session, finished),
                )
            )
            artifact = ArtifactRow(
                id=new_id("art"),
                case_id=finished.case_id,
                kind=ArtifactKind.editor_handoff.value,
                uri=handoff.package_uri,
                sha256=handoff.sha256,
                size_bytes=handoff.size_bytes,
                payload_schema="EditorHandoffPackageArtifact.v1",
                payload=handoff.manifest,
            )
            session.add(artifact)
            session.commit()
            session.refresh(artifact)
            return EditorHandoffPackageArtifact(
                package_artifact=artifact_ref_from_row(artifact),
                manifest=handoff.manifest,
            )

    def create_jianying_draft(
        self, finished_video_id: str, payload: CreateJianyingDraftRequest
    ) -> JianyingDraftPackageArtifact:
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, "Finished video is missing.")
            timeline_plan = self._timeline_plan_payload(session, finished_video_id)
            portrait_plan = self._latest_run_artifact_payload(session, finished.run_id, ArtifactKind.plan_portrait)
            broll_plan = self._latest_run_artifact_payload(session, finished.run_id, ArtifactKind.plan_broll)
            style_plan = self._latest_run_artifact_payload(session, finished.run_id, ArtifactKind.plan_style)
            caption_composition = self._latest_run_artifact_payload(
                session, finished.run_id, ArtifactKind.plan_caption_composition
            )
            caption_plan = (
                CaptionCompositionPlanArtifact.model_validate(caption_composition)
                if caption_composition is not None
                else None
            )
            audio_path = self._latest_run_artifact_path(session, finished.run_id, ArtifactKind.audio_tts)
            narration_units = self._narration_units(session, finished.run_id)
            jianying = JianyingDraftBuilder(self.object_store).build(
                JianyingDraftInput(
                    finished_video_id=finished_video_id,
                    title=finished.title,
                    video_path=self._artifact_path(session, ArtifactRef.model_validate(finished.video_artifact)),
                    audio_path=audio_path,
                    subtitle_path=(
                        self._artifact_path(session, ArtifactRef.model_validate(finished.subtitle_artifact))
                        if finished.subtitle_artifact
                        else None
                    ),
                    duration_sec=finished.duration_sec,
                    template_id=payload.template_id,
                    timeline_plan=timeline_plan,
                    narration_units=narration_units,
                    video_segments=build_video_segments_from_plans(
                        timeline_plan,
                        portrait_plan,
                        broll_plan,
                        resolve_source_path=lambda asset_id: self._media_asset_source_path(session, asset_id),
                    ),
                    audio_segments=build_audio_segments_from_sources(
                        audio_path,
                        finished.duration_sec,
                        style_plan,
                        resolve_source_path=lambda asset_id: self._media_asset_source_path(session, asset_id),
                    ),
                    text_segments=build_text_segments_from_narration(
                        narration_units, caption_plan
                    ),
                )
            )
            artifact = ArtifactRow(
                id=new_id("art"),
                case_id=finished.case_id,
                run_id=finished.run_id,
                kind=ArtifactKind.jianying_draft.value,
                uri=jianying.package_uri,
                sha256=jianying.sha256,
                size_bytes=jianying.size_bytes,
                payload_schema="JianyingDraftPackageArtifact.v1",
                payload=jianying.manifest,
            )
            session.add(artifact)
            session.commit()
            session.refresh(artifact)
            download = self.object_store.signed_url(jianying.package_uri)
            return JianyingDraftPackageArtifact(
                package_artifact=artifact_ref_from_row(artifact),
                draft_manifest=jianying.manifest,
                download_url=download.url,
                download_expires_at=download.expires_at,
            )

    def latest_jianying_draft(self, finished_video_id: str) -> JianyingDraftPackageArtifact | None:
        with self.session_factory() as session:
            artifacts = session.scalars(
                select(ArtifactRow)
                .where(
                    ArtifactRow.kind == ArtifactKind.jianying_draft.value,
                    ArtifactRow.payload.contains({"finished_video_id": finished_video_id}),
                )
                .order_by(ArtifactRow.created_at.desc())
            ).all()
            artifact = next(
                (
                    candidate
                    for candidate in artifacts
                    if candidate.uri
                    and isinstance(candidate.payload, dict)
                    and candidate.payload.get("portable_resources") is True
                ),
                None,
            )
            if artifact is None or not artifact.uri or not isinstance(artifact.payload, dict):
                return None
            download = self.object_store.signed_url(artifact.uri)
            return JianyingDraftPackageArtifact(
                package_artifact=artifact_ref_from_row(artifact),
                draft_manifest=artifact.payload,
                download_url=download.url,
                download_expires_at=download.expires_at,
            )

    def _artifact_path(self, session: Session, artifact_ref: ArtifactRef) -> Path:
        artifact = session.get(ArtifactRow, artifact_ref.artifact_id)
        if artifact is None or not artifact.uri:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Artifact URI is missing.")
        try:
            return local_object_path(self.object_store, artifact.uri)
        except ValueError as exc:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Artifact URI is not locally readable.") from exc

    def _latest_run_artifact_path(self, session: Session, run_id: str | None, kind: ArtifactKind) -> Path | None:
        if run_id is None:
            return None
        artifact = session.scalar(
            select(ArtifactRow)
            .where(ArtifactRow.run_id == run_id, ArtifactRow.kind == kind.value, ArtifactRow.uri.is_not(None))
            .order_by(ArtifactRow.created_at.desc())
        )
        if artifact is None:
            return None
        return self._artifact_path(session, artifact_ref_from_row(artifact))

    def _latest_run_artifact_payload(self, session: Session, run_id: str | None, kind: ArtifactKind) -> dict | None:
        if run_id is None:
            return None
        artifact = session.scalar(
            select(ArtifactRow)
            .where(ArtifactRow.run_id == run_id, ArtifactRow.kind == kind.value)
            .order_by(ArtifactRow.created_at.desc())
        )
        payload = artifact.payload if artifact is not None and isinstance(artifact.payload, dict) else None
        return payload

    def _media_asset_source_path(self, session: Session, asset_id: str) -> Path:
        asset = session.get(MediaAssetRow, asset_id)
        if asset is None or not asset.source_artifact_id:
            raise NodeExecutionError(ErrorCode.artifact_missing, f"Media asset source is missing: {asset_id}")
        artifact = session.get(ArtifactRow, asset.source_artifact_id)
        if artifact is None or not artifact.uri:
            raise NodeExecutionError(ErrorCode.artifact_missing, f"Media asset source artifact is missing: {asset_id}")
        return self._artifact_path(session, artifact_ref_from_row(artifact))

    def _timeline_plan_payload(self, session: Session, finished_video_id: str) -> dict | None:
        version = session.scalar(
            select(VideoVersionRow)
            .where(VideoVersionRow.finished_video_id == finished_video_id)
            .order_by(VideoVersionRow.created_at.desc())
        )
        if version is None:
            return None
        artifact = session.get(ArtifactRow, version.timeline_plan_artifact_id)
        return artifact.payload if artifact is not None and isinstance(artifact.payload, dict) else None

    def _narration_units(self, session: Session, run_id: str | None) -> list[dict]:
        if run_id is None:
            return []
        artifact = session.scalar(
            select(ArtifactRow)
            .where(ArtifactRow.run_id == run_id, ArtifactRow.kind == ArtifactKind.narration_units.value)
            .order_by(ArtifactRow.created_at.desc())
        )
        payload = artifact.payload if artifact is not None and isinstance(artifact.payload, dict) else {}
        units = payload.get("units") if isinstance(payload, dict) else None
        return list(units or [])

    def _handoff_assets(self, session: Session, finished: FinishedVideoRow) -> list[EditorHandoffAsset]:
        video_ref = ArtifactRef.model_validate(finished.video_artifact)
        assets = [self._handoff_asset(session, "video", video_ref)]
        if finished.cover_artifact:
            assets.append(self._handoff_asset(session, "cover", ArtifactRef.model_validate(finished.cover_artifact)))
        if finished.subtitle_artifact:
            assets.append(self._handoff_asset(session, "subtitle", ArtifactRef.model_validate(finished.subtitle_artifact)))
        return assets

    def _handoff_effects(self, session: Session, finished: FinishedVideoRow) -> dict:
        if not finished.run_id:
            return {}
        style_plan = self._latest_run_artifact_payload(
            session, finished.run_id, ArtifactKind.plan_style
        ) or {}
        style_dict = style_plan if isinstance(style_plan, dict) else {}
        caption_composition = self._latest_run_artifact_payload(
            session, finished.run_id, ArtifactKind.plan_caption_composition
        ) or {}
        broll_plan = self._latest_run_artifact_payload(
            session, finished.run_id, ArtifactKind.plan_broll
        ) or {}
        timeline_plan = self._timeline_plan_payload(session, finished.id) or {}
        overlays = [
            {
                "overlay_id": item.get("overlay_id"),
                "asset_id": item.get("asset_id"),
                "clip_id": item.get("clip_id"),
                "placement": item.get("placement"),
            }
            for item in (broll_plan.get("overlays") or [])
            if isinstance(item, dict)
            and item.get("placement") is not None
        ]
        tracks = [
            {
                "segment_id": item.get("segment_id"),
                "placement": item.get("placement"),
            }
            for item in (timeline_plan.get("tracks") or [])
            if isinstance(item, dict)
            and item.get("track_id") == "broll"
            and item.get("placement") is not None
        ]
        return {
            "subtitle": style_dict.get("subtitle") if isinstance(style_dict.get("subtitle"), dict) else {},
            "caption_composition": caption_composition,
            "broll_overlays": overlays,
            "timeline_tracks": tracks,
            "manual_acceptance": {
                "jianying_effect_id_policy": "If native Jianying effect_id changes, keep cutflow_effects/effects.json as source of truth and record a degradation on mismatch.",
                "requires_review": bool(overlays or tracks),
            },
        }

    def _handoff_asset(self, session: Session, role: str, artifact_ref: ArtifactRef) -> EditorHandoffAsset:
        return EditorHandoffAsset(
            role=role,
            artifact_id=artifact_ref.artifact_id,
            kind=artifact_ref.kind.value,
            source_path=self._artifact_path(session, artifact_ref),
        )

    def case_performance(self, *, case_id: str, window: str = "7d") -> CasePerformanceResponse:
        with self.session_factory() as session:
            statement = (
                select(PerformanceObservationRow)
                .where(PerformanceObservationRow.case_id == case_id)
                .order_by(PerformanceObservationRow.observed_at.desc())
            )
            observations = [performance_observation_row_to_contract(row) for row in session.scalars(statement)]
            obs_ids = {obs.id for obs in observations}
            scores = [
                performance_score_row_to_contract(row)
                for row in session.scalars(
                    select(PerformanceScoreRow).where(PerformanceScoreRow.case_id == case_id)
                )
                if row.observation_id in obs_ids
            ]
        return CasePerformanceResponse(
            metrics=PerformanceMetricView(
                impressions=int(sum(item.metric_value for item in observations if item.metric_name == "impressions")),
                views=int(sum(item.metric_value for item in observations if item.metric_name == "views")),
                likes=int(sum(item.metric_value for item in observations if item.metric_name == "likes")),
            ),
            observations=observations,
            scores=scores,
        )

    def import_metrics(
        self, *, case_id: str, payload: MetricsImportRequest, request_id: str
    ) -> ImportBatchReport:
        results: list[ImportRowResult] = []
        with self.session_factory() as session:
            records = [
                metrics_import.PublishRecordIndex(
                    publish_record_id=row.id,
                    video_version_id=row.video_version_id,
                    platform=row.platform,
                )
                for row in session.scalars(
                    select(PublishRecordRow).where(PublishRecordRow.case_id == case_id)
                )
            ]
            match = metrics_import.match_metrics_rows(
                payload.rows,
                policy=payload.matching_policy,
                records=records,
                default_platform=payload.platform,
                default_account_id=payload.account_id,
            )
            for matched in match.matched:
                # Build the contract directly from the match (mirrors the in-memory
                # path) so created_at/updated_at/schema_version come from EntityMeta
                # defaults — we never round-trip an unflushed ORM row through the
                # contract mapper (whose timestamp columns are still None pre-flush).
                observation = metrics_import.observation_contract_from_match(case_id, matched)
                if not payload.dry_run:
                    session.add(performance_observation_to_row(observation))
                    score = evolution.compute_performance_score(observation)
                    session.add(performance_score_to_row(score))
                results.append(
                    ImportRowResult(row_index=matched.row_index, status="created", internal_id=observation.id)
                )
            for unmatched in match.unmatched:
                results.append(
                    ImportRowResult(
                        row_index=unmatched.row_index,
                        status="skipped",
                        error=NodeError(code=ErrorCode.validation_invalid_options, message=unmatched.reason),
                    )
                )
            results.sort(key=lambda item: item.row_index)
            report = ImportBatchReport(
                batch_id=new_id("imp"),
                import_type="performance",
                status=ImportBatchStatus.completed
                if not match.unmatched
                else ImportBatchStatus.partially_failed,
                created_count=len(match.matched),
                skipped_count=len(match.unmatched),
                failed_count=0,
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
            feature_row = session.scalars(
                select(CreativeFeatureVectorRow)
                .where(CreativeFeatureVectorRow.video_version_id == video_version_id)
                .order_by(CreativeFeatureVectorRow.updated_at.desc())
                .limit(1)
            ).first()
            feature_vector = (
                creative_feature_vector_row_to_contract(feature_row)
                if feature_row is not None
                else self._extract_feature_vector(session, version)
            )
            return PerformanceAttributionResponse(
                video_version_id=video_version_id,
                feature_vector=feature_vector,
                observations=observations,
                contributing_memories=[],
            )

    def _extract_feature_vector(self, session: Session, version: VideoVersionRow) -> CreativeFeatureVector:
        """Derive a CreativeFeatureVector on-the-fly (§25.5) when none is persisted."""
        feature_id = f"cfv_{version.id}"
        script_row = (
            session.get(ScriptVersionRow, version.script_version_id)
            if version.script_version_id
            else None
        )
        partial: CreativeFeatureVector | None = None
        if script_row is not None:
            partial = evolution.extract_script_features(
                script_version_row_to_contract(script_row),
                case_id=version.case_id,
                feature_id=feature_id,
            )
        return evolution.extract_video_features(
            video_version_row_to_contract(version),
            feature_id=feature_id,
            partial=partial,
        )

    def create_import_batch(self, payload: CreateImportBatchRequest, request_id: str) -> ImportBatchReport | None:
        if payload.import_type not in SUPPORTED_IMPORT_TYPES:
            return None
        results: list[ImportRowResult] = []
        created = 0
        skipped = 0
        failed = 0
        # Track (case_id, video_number) already taken within THIS batch so a duplicate
        # number across two rows fails that row instead of poisoning the whole-batch commit.
        seen_finished_numbers: set[tuple[str, str]] = set()
        with self.session_factory() as session:
            for index, row in enumerate(payload.rows or []):
                if not isinstance(row, dict):
                    failed += 1
                    results.append(self._failed_row(index, "Import row must be an object."))
                    continue
                internal_id = new_id(payload.import_type)
                row_status = "created"
                result_internal_id = internal_id
                if payload.import_type == "media" and not _optional_str(row.get("uri")):
                    failed += 1
                    results.append(
                        self._failed_row(
                            index,
                            "Media import row requires uri.",
                            external_id=str(row.get("external_id")) if row.get("external_id") else None,
                        )
                    )
                    continue
                if not payload.dry_run:
                    try:
                        row_status, result_internal_id = self._create_import_row(
                            session,
                            payload.import_type,
                            internal_id,
                            row,
                            seen_finished_numbers=seen_finished_numbers,
                        )
                    except _ImportRowConflict as exc:
                        failed += 1
                        results.append(
                            self._failed_row(
                                index,
                                str(exc),
                                external_id=str(row.get("external_id")) if row.get("external_id") else None,
                                code=ErrorCode.validation_conflict,
                            )
                        )
                        continue
                if row_status == "skipped":
                    skipped += 1
                else:
                    created += 1
                results.append(
                    ImportRowResult(
                        row_index=index,
                        status=row_status,
                        external_id=str(row.get("external_id")) if row.get("external_id") else None,
                        internal_id=result_internal_id,
                    )
                )
            report = ImportBatchReport(
                batch_id=new_id("imp"),
                import_type=payload.import_type,
                status=ImportBatchStatus.completed if failed == 0 else ImportBatchStatus.partially_failed,
                created_count=created,
                skipped_count=skipped,
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

    def _create_import_row(
        self,
        session: Session,
        import_type: str,
        internal_id: str,
        row: dict,
        *,
        seen_finished_numbers: set[tuple[str, str]] | None = None,
    ) -> tuple[str, str]:
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
            case_id = str(row.get("case_id")) if row.get("case_id") else None
            title = str(row.get("title", "Imported media"))
            # Converge legacy visual kinds (portrait/broll) onto ``video`` so an
            # import manifest can never re-introduce a legacy asset-kind row into
            # ``media_assets`` (a plain string column with no enum constraint).
            kind, legacy_kind_tag = normalize_visual_asset_kind(
                str(row.get("kind", "other"))
            )
            uri = _optional_str(row.get("uri"))
            sha256 = _optional_str(row.get("sha256"))
            existing_asset = self._find_existing_imported_media_asset(
                session,
                case_id=case_id,
                kind=kind,
                sha256=sha256,
                uri=uri,
            )
            if existing_asset is not None:
                return "skipped", existing_asset.id
            artifact = self._create_imported_media_source_artifact(
                row=row,
                case_id=case_id,
                title=title,
                kind=kind,
                uri=uri,
                sha256=sha256,
            )
            session.add(artifact)
            session.flush()
            session.add(
                MediaAssetRow(
                    id=internal_id,
                    case_id=case_id,
                    title=title,
                    kind=kind,
                    source_artifact_id=artifact.id,
                    tags=(
                        [str(item) for item in tags] if isinstance(tags, list) else []
                    )
                    + ([legacy_kind_tag] if legacy_kind_tag else []),
                    annotation_status=str(row.get("annotation_status", "pending")),
                    usable=bool(row.get("usable", True)),
                    thumbnail_uri=_optional_str(row.get("thumbnail_uri"))
                    or _optional_str(row.get("thumbnail")),
                    duration_sec=_optional_float(row.get("duration_sec")),
                    width=_optional_int(row.get("width")),
                    height=_optional_int(row.get("height")),
                )
            )
        elif import_type == "finished_video":
            case_id = str(row.get("case_id", "case_demo"))
            video_number = _optional_str(row.get("video_number"))
            if video_number is not None:
                # uq_finished_videos_case_video_number (case_id, video_number) would raise
                # IntegrityError at the whole-batch commit, aborting EVERY row. Detect the
                # collision up front (vs. committed rows + earlier rows in this batch) and
                # fail just this row. NULL video_number stays unconstrained (multi-NULL ok).
                key = (case_id, video_number)
                if (seen_finished_numbers is not None and key in seen_finished_numbers) or (
                    self._finished_video_number_exists(session, case_id, video_number)
                ):
                    raise _ImportRowConflict(
                        f"finished video number {video_number!r} already exists for case "
                        f"{case_id!r}; imported numbers must be unique per case."
                    )
                if seen_finished_numbers is not None:
                    seen_finished_numbers.add(key)
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
                    owner_user_id=_optional_str(row.get("owner_user_id")),
                    title=str(row.get("title", "Imported finished video")),
                    video_number=video_number,
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
            status = str(normalize_publish_record_status(row.get("status", "published")))
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
        return "created", internal_id

    def _find_existing_imported_media_asset(
        self,
        session: Session,
        *,
        case_id: str | None,
        kind: str,
        sha256: str | None,
        uri: str,
    ) -> MediaAssetRow | None:
        statement = (
            select(MediaAssetRow)
            .join(ArtifactRow, MediaAssetRow.source_artifact_id == ArtifactRow.id)
            .where(
                MediaAssetRow.case_id == case_id,
                MediaAssetRow.kind == kind,
                ArtifactRow.kind == ArtifactKind.uploaded_file.value,
            )
            .order_by(MediaAssetRow.created_at.asc())
            .limit(1)
        )
        if sha256:
            statement = statement.where(ArtifactRow.sha256 == sha256)
        else:
            statement = statement.where(ArtifactRow.uri == uri)
        return session.scalar(statement)

    def _create_imported_media_source_artifact(
        self,
        *,
        row: dict,
        case_id: str | None,
        title: str,
        kind: str,
        uri: str,
        sha256: str | None,
    ) -> ArtifactRow:
        artifact_data = imported_media_artifact_data(
            row,
            case_id=case_id,
            title=title,
            kind=kind,
            uri=uri,
            sha256=sha256,
            probed=self._probe_import_media_if_local(uri),
        )
        payload = artifact_data.payload
        return ArtifactRow(
            id=new_id("art"),
            case_id=case_id,
            kind=ArtifactKind.uploaded_file.value,
            uri=uri,
            size_bytes=payload["size_bytes"],
            sha256=sha256,
            media_info=artifact_data.media_info.model_dump(mode="json") if artifact_data.media_info is not None else None,
            payload_schema="UploadedFileArtifact.v1",
            payload=payload,
        )

    def _probe_import_media_if_local(self, uri: str) -> MediaInfo | None:
        if uri.startswith("s3://"):
            return None
        try:
            return probe_media(local_object_path(self.object_store, uri))
        except (FfmpegCommandError, OSError, ValueError):
            return None

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

    def _selection_ledger_row(self, entry: SelectionLedgerEntry) -> SelectionLedgerRow:
        return SelectionLedgerRow(
            id=entry.id,
            case_id=entry.case_id,
            run_id=entry.run_id,
            medium=entry.medium,
            asset_id=entry.asset_id,
            clip_id=entry.clip_id,
            slot_phase=entry.slot_phase,
            diversity_key=entry.diversity_key,
            created_at=entry.created_at,
        )

    def _selection_reservation_row(
        self, reservation: SelectionReservationRecord
    ) -> SelectionReservationRow:
        return SelectionReservationRow(
            id=reservation.id,
            case_id=reservation.case_id,
            run_id=reservation.run_id,
            medium=reservation.medium,
            asset_id=reservation.asset_id,
            diversity_key=reservation.diversity_key,
            status=reservation.status,
            created_at=reservation.created_at,
            expires_at=reservation.expires_at,
            committed_at=reservation.committed_at,
            released_at=reservation.released_at,
        )

    def _durable_invocation_progress(self, session: Session, run_id: str) -> dict[str, Any]:
        """Current durable progress for this run's invocations, in one query.

        Carries every field the Gateway writes out of band — status/external_job_id when
        it submits and polls, error/finished_at on a terminal write, updated_at on all of
        them — so the snapshot can decline to regress any of them.
        """
        rows = session.execute(
            select(
                ProviderInvocationRow.id,
                ProviderInvocationRow.status,
                ProviderInvocationRow.external_job_id,
                ProviderInvocationRow.error,
                ProviderInvocationRow.finished_at,
                ProviderInvocationRow.updated_at,
            ).where(ProviderInvocationRow.run_id == run_id)
        ).all()
        return {row.id: row for row in rows}

    def _preserve_durable_invocation_progress(
        self,
        row: ProviderInvocationRow,
        durable: Any | None,
    ) -> None:
        """Never let a stale snapshot copy regress what the Gateway already committed.

        The Gateway persists submit/polling/terminal transitions the moment they happen.
        A snapshot built from an older in-memory copy would otherwise merge an earlier
        status over the durable row, drop its external_job_id, and erase the terminal
        error detail (e.g. provider_submit_outcome_unknown). Accounting fields
        (usage/cost/duration/node_run_id) still merge normally.

        result_payload needs no guard here and must not get one: nothing in the run state
        carries it, so ``_provider_invocation_row`` never sets it, and a merge leaves an
        attribute the source never set alone. Reading it here would put None into the
        source's dict and create the very regression it would then have to repair.
        """
        if durable is None:
            return
        memory_rank = _PROVIDER_STATUS_RANK.get(row.status, 0)
        durable_rank = _PROVIDER_STATUS_RANK.get(durable.status, 0)
        if memory_rank < durable_rank:
            row.status = durable.status
            row.external_job_id = durable.external_job_id
            row.error = durable.error
            row.finished_at = durable.finished_at
        # updated_at must not move backwards even at an equal rank: the Gateway reads it
        # to decide whether an in-flight holder has gone stale (presumed dead), so a
        # rewind would make a live holder look abandoned and invite a takeover.
        if durable.updated_at is not None and (
            row.updated_at is None or row.updated_at < durable.updated_at
        ):
            row.updated_at = durable.updated_at

    def _provider_invocation_row(self, invocation: ProviderInvocation) -> ProviderInvocationRow:
        return ProviderInvocationRow(
            id=invocation.id,
            case_id=invocation.case_id,
            run_id=invocation.run_id,
            node_run_id=invocation.node_run_id,
            idempotency_key=invocation.idempotency_key,
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
        self, event: YieldFunnelEvent, case_id: str | None, owner_user_id: str | None = None
    ) -> YieldFunnelEventRow:
        return YieldFunnelEventRow(
            id=event.id,
            case_id=case_id,
            job_id=event.job_id,
            run_id=event.run_id,
            owner_user_id=owner_user_id,
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

    def _failure_taxonomy_row(
        self, entry: FailureTaxonomyEntry, dedupe_index: dict[str, str]
    ) -> FailureTaxonomyRow:
        # Recover the dedupe_key (held in the repo's side index, not on the contract).
        dedupe_key = next((key for key, eid in dedupe_index.items() if eid == entry.id), None)
        failure_class = (
            entry.failure_class.value
            if hasattr(entry.failure_class, "value")
            else str(entry.failure_class)
        )
        return FailureTaxonomyRow(
            id=entry.id,
            target_type=entry.target_type,
            target_id=entry.target_id,
            failure_class=failure_class,
            error_code=entry.error_code,
            run_id=entry.run_id,
            job_id=entry.job_id,
            case_id=entry.case_id,
            node_id=entry.node_id,
            message=entry.message,
            dedupe_key=dedupe_key,
            schema_version=entry.schema_version,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
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
            owner_user_id=finished.owner_user_id,
            title=finished.title,
            video_number=finished.video_number,
            video_artifact=finished.video_artifact.model_dump(mode="json"),
            cover_artifact=finished.cover_artifact.model_dump(mode="json") if finished.cover_artifact else None,
            cover_thumb_artifact=(
                finished.cover_thumb_artifact.model_dump(mode="json")
                if finished.cover_thumb_artifact
                else None
            ),
            subtitle_artifact=(
                finished.subtitle_artifact.model_dump(mode="json") if finished.subtitle_artifact else None
            ),
            duration_sec=finished.duration_sec,
            qc_status=finished.qc_status,
            lipsync_provider_id=finished.lipsync_provider_id,
            lipsync_fallback_used=finished.lipsync_fallback_used,
            lipsync_fallback_reason=finished.lipsync_fallback_reason,
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

    def _failed_row(
        self,
        index: int,
        message: str,
        *,
        external_id: str | None = None,
        code: ErrorCode = ErrorCode.validation_invalid_options,
    ) -> ImportRowResult:
        return ImportRowResult(
            row_index=index,
            status="failed",
            external_id=external_id,
            error=NodeError(code=code, message=message),
        )

    @staticmethod
    def _finished_video_number_exists(session: Session, case_id: str, video_number: str) -> bool:
        return (
            session.scalar(
                select(FinishedVideoRow.id)
                .where(
                    FinishedVideoRow.case_id == case_id,
                    FinishedVideoRow.video_number == video_number,
                )
                .limit(1)
            )
            is not None
        )
