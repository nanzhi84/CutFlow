from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.contracts import (
    ArtifactKind,
    ArtifactRef,
    NodeError,
    PublishAttempt,
    PublishBatchItemVm,
    PublishBatchVm,
    PublishDefaults,
    PublishPackage,
    PublishRecord,
)
from packages.core.storage.database import (
    ArtifactRow,
    PublishAttemptRow,
    PublishBatchItemRow,
    PublishBatchRow,
    PublishPackageRow,
    PublishRecordRow,
)


def artifact_ref_from_row(row: ArtifactRow) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=row.id,
        kind=ArtifactKind(row.kind),
        uri=row.uri or f"artifact://{row.id}",
        schema_version=row.schema_version,
        sha256=row.sha256,
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


def publish_item_row_to_contract(row: PublishBatchItemRow) -> PublishBatchItemVm:
    return PublishBatchItemVm(
        id=row.id,
        publish_package_id=row.publish_package_id,
        platform=row.platform,
        title=row.title,
        description=row.description,
        selected=row.selected,
        status=row.status,
        publish_content=row.publish_content or "",
        cover_title=row.cover_title or "",
        cover_subtitle=row.cover_subtitle or "",
        cover_artifact_id=row.cover_artifact_id,
        tags=list(row.tags or []),
        location=row.location,
        account_group=row.account_group,
        scheduled_at=row.scheduled_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def publish_batch_row_to_contract(session: Session, row: PublishBatchRow) -> PublishBatchVm:
    statement = (
        select(PublishBatchItemRow)
        .where(PublishBatchItemRow.batch_id == row.id)
        .order_by(PublishBatchItemRow.created_at.asc(), PublishBatchItemRow.id.asc())
    )
    items = [publish_item_row_to_contract(item) for item in session.scalars(statement)]
    return PublishBatchVm(
        id=row.id,
        status=row.status,
        items=items,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def publish_attempt_row_to_contract(row: PublishAttemptRow) -> PublishAttempt:
    return PublishAttempt(
        id=row.id,
        batch_id=row.batch_id,
        item_id=row.item_id,
        platforms=list(row.platforms or []),
        manual_review=row.manual_review,
        status=row.status,
        adapter_id=row.adapter_id,
        external_task_id=row.external_task_id,
        results=list(row.results or []),
        error=NodeError.model_validate(row.error) if row.error else None,
        finished_at=row.finished_at,
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
