from __future__ import annotations


from fastapi import APIRouter, Request

from apps.api.dependencies import require_role
from apps.api.services import secrets as service
from packages.core import contracts as c

router = APIRouter()

@router.get("/api/secrets", response_model=c.PageResponse[c.SecretPreview])
def list_secrets(request: Request, limit: int = 50) -> c.PageResponse[c.SecretPreview]:
    require_role(request, c.UserRole.admin)
    return service.list_secrets(request, limit)


@router.post("/api/secrets", response_model=c.SecretPreview, status_code=201)
def create_secret(payload: c.CreateSecretRequest, request: Request) -> c.SecretPreview:
    user = require_role(request, c.UserRole.admin)
    return service.create_secret(payload, request, actor=user.id)


@router.post("/api/secrets/{secret_id}/rotate", response_model=c.SecretPreview)
def rotate_secret(secret_id: str, payload: c.RotateSecretRequest, request: Request) -> c.SecretPreview:
    user = require_role(request, c.UserRole.admin)
    return service.rotate_secret(secret_id, payload, request, actor=user.id)


@router.patch("/api/secrets/{secret_id}/disable", response_model=c.SecretPreview)
def disable_secret(secret_id: str, payload: c.DisableSecretRequest, request: Request) -> c.SecretPreview:
    user = require_role(request, c.UserRole.admin)
    return service.disable_secret(secret_id, payload, request, actor=user.id)
