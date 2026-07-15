from __future__ import annotations


from fastapi import APIRouter, BackgroundTasks, Request

from apps.api.dependencies import require_role
from apps.api.services import uploads as service
from packages.core import contracts as c

router = APIRouter()


@router.post("/api/uploads/prepare", response_model=c.PrepareUploadResponse, status_code=201)
def prepare_upload(payload: c.PrepareUploadRequest, request: Request) -> c.PrepareUploadResponse:
    user = require_role(request, c.UserRole.operator)
    return service.prepare_upload(payload, request, user)


@router.post("/api/uploads/complete", response_model=c.CompleteUploadResponse)
def complete_upload(payload: c.CompleteUploadRequest, request: Request) -> c.CompleteUploadResponse:
    user = require_role(request, c.UserRole.operator)
    return service.complete_upload(payload, request, user)


@router.post(
    "/api/uploads/{upload_session_id}/parts/sign",
    response_model=c.SignUploadPartsResponse,
)
def sign_upload_parts(
    upload_session_id: str,
    payload: c.SignUploadPartsRequest,
    request: Request,
) -> c.SignUploadPartsResponse:
    user = require_role(request, c.UserRole.operator)
    return service.sign_upload_parts(upload_session_id, payload, request, user)


@router.get(
    "/api/uploads/{upload_session_id}/resume",
    response_model=c.ResumeUploadResponse,
)
def resume_upload(upload_session_id: str, request: Request) -> c.ResumeUploadResponse:
    user = require_role(request, c.UserRole.operator)
    return service.resume_upload(upload_session_id, request, user)


@router.post(
    "/api/uploads/{upload_session_id}/object-complete",
    response_model=c.ObjectCompleteUploadResponse,
    status_code=202,
)
def object_complete_upload(
    upload_session_id: str,
    payload: c.ObjectCompleteUploadRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> c.ObjectCompleteUploadResponse:
    user = require_role(request, c.UserRole.operator)
    return service.object_complete_upload(
        upload_session_id, payload, request, user, background_tasks
    )


@router.post("/api/uploads/{upload_session_id}/cancel", response_model=c.UploadSession)
def cancel_upload(upload_session_id: str, request: Request) -> c.UploadSession:
    user = require_role(request, c.UserRole.operator)
    return service.cancel_upload(upload_session_id, request, user)


@router.get("/api/uploads/{upload_session_id}", response_model=c.UploadSession)
def get_upload(upload_session_id: str, request: Request) -> c.UploadSession:
    user = require_role(request, c.UserRole.operator)
    return service.get_upload(upload_session_id, request, user)
