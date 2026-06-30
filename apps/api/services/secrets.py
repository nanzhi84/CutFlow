from __future__ import annotations


from fastapi import Request

from apps.api.common import (
    request_id,
    secret_repository,
)
from packages.core import contracts as c


def list_secrets(request: Request, limit: int = 50) -> c.PageResponse[c.SecretPreview]:
    values = secret_repository(request).list_secrets(limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_secret(
    payload: c.CreateSecretRequest, request: Request, actor: str | None = None
) -> c.SecretPreview:
    # The repo stages the audit row in the SAME transaction as the mutation
    # (spec §32.9), so we do NOT double-write a separate audit event.
    return secret_repository(request).create_secret(payload, actor=actor)


def rotate_secret(
    secret_id: str, payload: c.RotateSecretRequest, request: Request, actor: str | None = None
) -> c.SecretPreview:
    # The repo stages the audit row in the mutation transaction (spec §32.9).
    return secret_repository(request).rotate_secret(secret_id, payload, actor=actor)


def disable_secret(
    secret_id: str, payload: c.DisableSecretRequest, request: Request, actor: str | None = None
) -> c.SecretPreview:
    # The repo stages the audit row in the mutation transaction (spec §32.9).
    return secret_repository(request).disable_secret(secret_id, payload, actor=actor)
