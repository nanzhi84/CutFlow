from __future__ import annotations


from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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


@router.patch("/api/publish/packages/{package_id}", response_model=c.PublishPackage)
def patch_publish_package(
    package_id: str, payload: c.PatchPublishPackageRequest, request: Request
) -> c.PublishPackage | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.patch_publish_package(package_id, payload, request)


@router.get("/api/publish/batches", response_model=c.PageResponse[c.PublishBatchVm])
def publish_batches(
    request: Request, limit: int = 50, case_id: str | None = None
) -> c.PageResponse[c.PublishBatchVm]:

    return service.publish_batches(request, limit, case_id)


@router.post("/api/publish/batches", response_model=c.PublishBatchVm, status_code=201)
def create_publish_batch(payload: c.CreatePublishBatchRequest, request: Request) -> c.PublishBatchVm:
    require_role(request, c.UserRole.operator)
    return service.create_publish_batch(payload, request)


@router.get("/api/publish/batches/{batch_id}", response_model=c.PublishBatchVm)
def publish_batch_detail(request: Request, batch_id: str) -> c.PublishBatchVm | JSONResponse:

    return service.publish_batch_detail(request, batch_id)


@router.get("/api/publish/batches/{batch_id}/attempts", response_model=c.PageResponse[c.PublishAttempt])
def publish_batch_attempts(request: Request, batch_id: str, limit: int = 50) -> c.PageResponse[c.PublishAttempt] | JSONResponse:
    return service.publish_batch_attempts(request, batch_id, limit)


@router.delete("/api/publish/batches/{batch_id}", response_model=c.OkResponse)
def delete_publish_batch(
    batch_id: str, request: Request, payload: c.DeletePublishResourceRequest | None = None
) -> c.OkResponse | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.delete_publish_batch(batch_id, request)


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


@router.post(
    "/api/publish/batches/{batch_id}/items/{item_id}/generate-copy",
    response_model=c.PublishCopyResult,
)
def generate_publish_copy(
    batch_id: str, item_id: str, payload: c.GeneratePublishCopyRequest, request: Request
) -> c.PublishCopyResult | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.generate_publish_copy(batch_id, item_id, payload, request)


@router.post(
    "/api/publish/batches/{batch_id}/items/{item_id}/generate-cover",
    response_model=c.PublishCoverResult,
)
def generate_publish_cover(
    batch_id: str, item_id: str, payload: c.GeneratePublishCoverRequest, request: Request
) -> c.PublishCoverResult | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.generate_publish_cover(batch_id, item_id, payload, request)


@router.post(
    "/api/publish/batches/{batch_id}/items/{item_id}/preview-cover-frame",
    response_model=c.PreviewCoverFrameResult,
)
def preview_publish_cover_frame(
    batch_id: str, item_id: str, payload: c.PreviewCoverFrameRequest, request: Request
) -> c.PreviewCoverFrameResult | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.preview_publish_cover_frame(batch_id, item_id, payload, request)


@router.get("/api/publish/platform-accounts", response_model=c.PlatformAccountList)
def publish_platform_accounts(
    request: Request,
    account_group: str | None = None,
    case_name: str | None = None,
    adapter_id: str | None = None,
) -> c.PlatformAccountList:
    require_role(request, c.UserRole.operator)
    return service.platform_accounts(
        request, account_group=account_group, case_name=case_name, adapter_id=adapter_id
    )


@router.patch("/api/publish/items/{item_id}", response_model=c.PublishBatchItemVm)
def patch_publish_item(
    item_id: str, payload: c.PatchPublishItemRequest, request: Request
) -> c.PublishBatchItemVm | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.patch_publish_item(item_id, payload, request)


@router.delete("/api/publish/items/{item_id}", response_model=c.OkResponse)
def delete_publish_item(
    item_id: str, request: Request, payload: c.DeletePublishResourceRequest | None = None
) -> c.OkResponse | JSONResponse:
    require_role(request, c.UserRole.operator)
    return service.delete_publish_item(item_id, request)


@router.get("/api/publish/attempts/{attempt_id}", response_model=c.PublishAttemptDetail)
def publish_attempt(request: Request, attempt_id: str) -> c.PublishAttemptDetail | JSONResponse:

    return service.publish_attempt(request, attempt_id)
