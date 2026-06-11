from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Request, Response, UploadFile
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
from packages.core.auth import SqlAlchemyAuthService
from packages.core.contracts.state_machines import assert_transition
from packages.core.observability import metric_snapshot
from packages.core.registration_codes import hash_registration_code
from packages.core.storage.object_store import parse_local_uri
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError

def publish_packages(request: Request, limit: int = 50) -> c.PageResponse[c.PublishPackage]:

    if publishing_repository(request) is not None:
        values = publishing_repository(request).list_packages(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository(request).publish_packages.values(), limit)


def create_publish_package(payload: c.CreatePublishPackageRequest, request: Request) -> c.PublishPackage:
    if publishing_repository(request) is not None:
        return publishing_repository(request).create_package(payload)
    if payload.source_finished_video_id:
        return repository(request).create_publish_package_from_finished_video(
            repository(request).finished_videos[payload.source_finished_video_id],
            title=payload.title,
            description=payload.description,
        )
    if not payload.upload_artifact_id:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Upload artifact is required.")
    package = c.PublishPackage(
        id=new_id("pkg"),
        upload_artifact_id=payload.upload_artifact_id,
        video_artifact=ensure_artifact_ref(request, payload.upload_artifact_id),
        platform_defaults=c.PublishDefaults(title=payload.title, description=payload.description),
    )
    repository(request).publish_packages[package.id] = package
    return package


def publish_batches(request: Request, limit: int = 50) -> c.PageResponse[c.PublishBatchVm]:

    if publishing_repository(request) is not None:
        values = publishing_repository(request).list_batches(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository(request).publish_batches.values(), limit)


def create_publish_batch(payload: c.CreatePublishBatchRequest, request: Request) -> c.PublishBatchVm:
    if publishing_repository(request) is not None:
        return publishing_repository(request).create_batch(payload)
    return repository(request).create_publish_batch(payload.publish_package_ids, payload.platform_targets)


def publish_batch_detail(request: Request, batch_id: str) -> c.PublishBatchVm | JSONResponse:

    if publishing_repository(request) is not None:
        batch = publishing_repository(request).get_batch(batch_id)
        if batch is None:
            return not_found_response("Publish batch not found")
        return batch
    batch = repository(request).publish_batches.get(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    return batch


def submit_publish_batch(
    batch_id: str, payload: c.SubmitPublishBatchRequest, request: Request
) -> c.PublishBatchVm | JSONResponse:
    if publishing_repository(request) is not None:
        batch = publishing_repository(request).submit_batch(batch_id, payload)
        if batch is None:
            return not_found_response("Publish batch not found")
        return batch
    batch = repository(request).publish_batches.get(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    new_items = []
    selected_count = 0
    for item in batch.items:
        if not item.selected:
            new_items.append(item)
            continue
        selected_count += 1
        current_item_status = item.status
        for next_status in ["normalizing", "asr_running", "copy_running", "cover_running", "review_ready"]:
            assert_transition("publish_item", current_item_status, next_status)
            current_item_status = next_status
        if not payload.dry_run:
            assert_transition("publish_item", current_item_status, "publishing")
            current_item_status = "publishing"
            if payload.simulate_publish_failure:
                assert_transition("publish_item", current_item_status, "publish_failed")
                current_item_status = "publish_failed"
            else:
                assert_transition("publish_item", current_item_status, "published")
                current_item_status = "published"
        new_items.append(
            item.model_copy(
                update={"status": c.PublishItemStatus(current_item_status), "updated_at": c.utcnow()}
            )
        )
        attempt_status = "manual_review_ready" if payload.dry_run else (
            "failed" if payload.simulate_publish_failure else "published"
        )
        assert_transition("publish_attempt", "created", attempt_status)
        attempt = c.PublishAttempt(
            id=new_id("pub_attempt"),
            batch_id=batch.id,
            item_id=item.id,
            platforms=[item.platform],
            manual_review=payload.dry_run,
            status=c.PublishAttemptStatus(attempt_status),
            adapter_id="sandbox.publish",
            results=[],
            error=(
                c.NodeError(
                    code=c.ErrorCode.publish_failed,
                    message="Sandbox publish adapter simulated a failed publish.",
                    retryable=True,
                )
                if payload.simulate_publish_failure
                else None
            ),
            finished_at=c.utcnow() if attempt_status == "published" else None,
        )
        repository(request).publish_attempts[attempt.id] = attempt
    if selected_count == 0:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "At least one publish item must be selected.")
    assert_transition("publish_batch", batch.status, "processing")
    next_batch_status = "review_ready" if payload.dry_run else "publishing"
    assert_transition("publish_batch", "processing", next_batch_status)
    if not payload.dry_run:
        if payload.simulate_publish_failure:
            assert_transition("publish_batch", next_batch_status, "partial_failed")
            next_batch_status = "partial_failed"
        else:
            assert_transition("publish_batch", next_batch_status, "completed")
            next_batch_status = "completed"
    batch = batch.model_copy(
        update={"status": c.PublishBatchStatus(next_batch_status), "items": new_items, "updated_at": c.utcnow()}
    )
    repository(request).publish_batches[batch.id] = batch
    return batch


def retry_publish_item(batch_id: str, item_id: str, request: Request) -> c.PublishBatchItemVm | JSONResponse:
    if publishing_repository(request) is not None:
        return not_found_response("retry-publish is only implemented by the local sandbox adapter")
    batch = repository(request).publish_batches.get(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    for index, item in enumerate(batch.items):
        if item.id != item_id:
            continue
        if item.status != c.PublishItemStatus.publish_failed:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Publish item is not failed.")
        current_item_status = item.status
        assert_transition("publish_item", current_item_status, "publishing")
        current_item_status = "publishing"
        assert_transition("publish_item", current_item_status, "published")
        updated = item.model_copy(update={"status": c.PublishItemStatus.published, "updated_at": c.utcnow()})
        items = list(batch.items)
        items[index] = updated
        next_batch_status = batch.status
        if next_batch_status == c.PublishBatchStatus.partial_failed:
            assert_transition("publish_batch", next_batch_status, "publishing")
            next_batch_status = c.PublishBatchStatus.publishing
        if all(existing.status == c.PublishItemStatus.published for existing in items):
            assert_transition("publish_batch", next_batch_status, "completed")
            next_batch_status = c.PublishBatchStatus.completed
        repository(request).publish_batches[batch.id] = batch.model_copy(
            update={"status": next_batch_status, "items": items, "updated_at": c.utcnow()}
        )
        attempt = c.PublishAttempt(
            id=new_id("pub_attempt"),
            batch_id=batch.id,
            item_id=item.id,
            platforms=[item.platform],
            manual_review=False,
            status=c.PublishAttemptStatus.published,
            adapter_id="sandbox.publish",
            results=[{"retry": True}],
            finished_at=c.utcnow(),
        )
        repository(request).publish_attempts[attempt.id] = attempt
        return updated
    return not_found_response("Publish item not found")


def patch_publish_item(
    item_id: str, payload: c.PatchPublishItemRequest, request: Request
) -> c.PublishBatchItemVm | JSONResponse:
    if publishing_repository(request) is not None:
        item = publishing_repository(request).patch_item(item_id, payload)
        if item is None:
            return not_found_response("Publish item not found")
        return item
    for batch in repository(request).publish_batches.values():
        for index, item in enumerate(batch.items):
            if item.id == item_id:
                updated = item.model_copy(update={**payload.model_dump(exclude_none=True), "updated_at": c.utcnow()})
                items = list(batch.items)
                items[index] = updated
                repository(request).publish_batches[batch.id] = batch.model_copy(update={"items": items})
                return updated
    return not_found_response("Publish item not found")


def publish_attempt(request: Request, attempt_id: str) -> c.PublishAttemptDetail | JSONResponse:

    if publishing_repository(request) is not None:
        detail = publishing_repository(request).attempt_detail(attempt_id)
        if detail is None:
            return not_found_response("Publish attempt not found")
        return detail
    attempt = repository(request).publish_attempts.get(attempt_id)
    if attempt is None:
        return not_found_response("Publish attempt not found")
    return c.PublishAttemptDetail(attempt=attempt, record=None)
