"""Batch annotation (批量标注) endpoint + execution.

Creates an ``annotation_batch`` :class:`Job` from an :class:`AnnotationBatchRequest`
and fans the SAME single-asset gated runner used by '重新分析/rerun' over the
``asset_ids``, so batch annotation is a real end-to-end flow. Each asset is
annotated independently so one bad asset can't sink the batch.

Behavior:

- ``force=False`` (default): an asset already in ``annotation_status=annotated`` is
  skipped (status ``skipped``); ``force=True`` re-annotates every asset.
- per-asset success persists a real AnnotationV4 canonical (the must-retain '素材 AI
  标注' artifact) and reports ``material.annotation``; a failed VLM run reports
  ``material.annotation_failed`` (the asset is marked ``annotation_failed``, never
  entering the usable pool).
"""

from __future__ import annotations

import logging

from fastapi import Request

from apps.api.common import media_repository, production_repository, repository, request_id
from apps.api.dependencies import current_user
from apps.api.services import asset_annotation
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.media.annotation import SensorDeps

logger = logging.getLogger("apps.api.services.annotation_batch")


def run_batch_annotation(
    payload: c.AnnotationBatchRequest,
    request: Request,
    *,
    sensor_deps: SensorDeps | None = None,
) -> c.AnnotationBatchResponse:
    """Create an annotation_batch Job and run the gated runner over every asset_id."""
    if not payload.asset_ids:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "asset_ids 不能为空。")

    job = c.Job(
        id=new_id("job"),
        type=c.JobType.annotation_batch,
        case_id=None,
        created_by=current_user(request).id,
        request_schema=payload.schema_version,
        request=payload,
        status=c.JobStatus.running,
    )
    _persist_job(request, job)

    results: list[c.AnnotationBatchResultItem] = []
    for asset_id in payload.asset_ids:
        results.append(
            _annotate_one(
                request,
                asset_id,
                provider_profile_id=payload.provider_profile_id,
                force=payload.force,
                material_type=payload.material_type,
                sensor_deps=sensor_deps,
            )
        )

    completed = sum(1 for r in results if r.status == "completed")
    failed = sum(1 for r in results if r.status == "failed")
    skipped = sum(1 for r in results if r.status == "skipped")
    _finish_job(request, job, failed=failed)
    return c.AnnotationBatchResponse(
        job_id=job.id,
        results=results,
        completed_count=completed,
        failed_count=failed,
        skipped_count=skipped,
        request_id=request_id(),
    )


def _annotate_one(
    request: Request,
    asset_id: str,
    *,
    provider_profile_id: str | None,
    force: bool,
    material_type: str | None,
    sensor_deps: SensorDeps | None,
) -> c.AnnotationBatchResultItem:
    rerun_payload = c.RerunAnnotationRequest(provider_profile_id=provider_profile_id, force=force)
    try:
        asset = _asset_record(request, asset_id)
        if asset is None:
            return c.AnnotationBatchResultItem(
                asset_id=asset_id,
                status="failed",
                error_code=c.ErrorCode.artifact_missing,
                message="素材不存在。",
            )
        current_status = asset.annotation_status
        if material_type and asset.kind != material_type:
            return c.AnnotationBatchResultItem(
                asset_id=asset_id,
                status="skipped",
                annotation_status=current_status,
                message=f"material_type 不匹配（{asset.kind} != {material_type}），跳过。",
            )
        if not force and current_status == "annotated":
            return c.AnnotationBatchResultItem(
                asset_id=asset_id, status="skipped", annotation_status="annotated", message="已标注，跳过。"
            )
        response = asset_annotation.run_sqlalchemy_asset_annotation(
            request, asset_id, rerun_payload, sensor_deps=sensor_deps
        )
        if response is None:
            return c.AnnotationBatchResultItem(
                asset_id=asset_id,
                status="failed",
                error_code=c.ErrorCode.artifact_missing,
                message="素材不存在。",
            )
        new_status = _current_annotation_status(request, asset_id)
        if response.status == "failed":
            # material.annotation_failed: VLM ran but exhausted retries.
            return c.AnnotationBatchResultItem(
                asset_id=asset_id,
                status="failed",
                annotation_status=new_status,
                error_code=c.ErrorCode.material_annotation_failed,
                message="标注失败，素材标记 annotation_failed。",
            )
        # material.annotation: real or degraded (sensor-only) annotation persisted.
        return c.AnnotationBatchResultItem(
            asset_id=asset_id, status="completed", annotation_status=new_status
        )
    except NodeExecutionError as exc:
        logger.warning("[batch-annotation] asset %s failed: %s", asset_id, exc.error.message)
        return c.AnnotationBatchResultItem(
            asset_id=asset_id, status="failed", error_code=exc.error.code, message=exc.error.message
        )


def _asset_record(request: Request, asset_id: str) -> c.MediaAssetRecord | None:
    return media_repository(request).asset_record(asset_id)


def _current_annotation_status(request: Request, asset_id: str) -> str | None:
    asset = _asset_record(request, asset_id)
    return asset.annotation_status if asset is not None else None


def _persist_job(request: Request, job: c.Job) -> None:
    repository(request).jobs[job.id] = job
    production_repository(request).persist_job(job)


def _finish_job(request: Request, job: c.Job, *, failed: int) -> None:
    status = c.JobStatus.failed if failed else c.JobStatus.succeeded
    finished = job.model_copy(update={"status": status, "updated_at": c.utcnow()})
    _persist_job(request, finished)
