from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from apps.api.dependencies import require_role
from apps.api.services import publishing as service
from packages.core import contracts as c

router = APIRouter()

@router.get("/api/publish/packages", response_model=c.PageResponse[c.PublishPackage])
def publish_packages(request: Request, limit: int = 50) -> c.PageResponse[c.PublishPackage]:

    return service.publish_packages(request, limit)


@router.post("/api/publish/packages", response_model=c.PublishPackage, status_code=201)
def create_publish_package(payload: c.CreatePublishPackageRequest, request: Request) -> c.PublishPackage:
    require_role(request, c.UserRole.operator)
    return service.create_publish_package(payload, request)


@router.get("/api/publish/batches", response_model=c.PageResponse[c.PublishBatchVm])
def publish_batches(request: Request, limit: int = 50) -> c.PageResponse[c.PublishBatchVm]:

    return service.publish_batches(request, limit)


@router.post("/api/publish/batches", response_model=c.PublishBatchVm, status_code=201)
def create_publish_batch(payload: c.CreatePublishBatchRequest, request: Request) -> c.PublishBatchVm:
    require_role(request, c.UserRole.operator)
    return service.create_publish_batch(payload, request)


@router.get("/api/publish/batches/{batch_id}", response_model=c.PublishBatchVm)
def publish_batch_detail(request: Request, batch_id: str) -> c.PublishBatchVm | JSONResponse:

    return service.publish_batch_detail(request, batch_id)


@router.post("/api/publish/batches/{batch_id}/submit", response_model=c.PublishBatchVm, status_code=202)
def submit_publish_batch(
    batch_id: str, payload: c.SubmitPublishBatchRequest, request: Request
) -> c.PublishBatchVm | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.submit_publish_batch(batch_id, payload, request)


@router.post(
    "/api/publish/batches/{batch_id}/items/{item_id}/retry-publish",
    response_model=c.PublishBatchItemVm,
)
def retry_publish_item(batch_id: str, item_id: str, request: Request) -> c.PublishBatchItemVm | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.retry_publish_item(batch_id, item_id, request)


@router.patch("/api/publish/items/{item_id}", response_model=c.PublishBatchItemVm)
def patch_publish_item(
    item_id: str, payload: c.PatchPublishItemRequest, request: Request
) -> c.PublishBatchItemVm | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.patch_publish_item(item_id, payload, request)


@router.get("/api/publish/attempts/{attempt_id}", response_model=c.PublishAttemptDetail)
def publish_attempt(request: Request, attempt_id: str) -> c.PublishAttemptDetail | JSONResponse:

    return service.publish_attempt(request, attempt_id)
