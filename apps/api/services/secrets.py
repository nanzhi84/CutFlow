from __future__ import annotations


from fastapi import Request

from apps.api.common import (
    page,
    repository,
    request_id,
    secret_repository,
    secret_store,
)
from packages.core import contracts as c
from packages.core.storage.repository import new_id


def _record_secret_audit(
    request: Request,
    *,
    action: str,
    secret_id: str | None,
    secret_ref: str | None = None,
    provider_id: str | None = None,
    environment: str | None = None,
    actor: str | None = None,
) -> None:
    """Append a secret governance audit event for the IN-MEMORY backend (spec §11.3 / §32.9).

    Captures actor, action, secret_ref/profile and timestamp; the secret VALUE is
    never recorded. The DB backend does NOT use this seam — it stages the audit row
    in the SAME transaction as the mutation (see sqlalchemy_secrets.py) so the audit
    cannot succeed/fail independently of the governance op (spec §32.9 fail-closed).
    This in-memory path exists only because there is no transaction to join.
    """
    # Spec 11.3: never log the plaintext / reversible value — only metadata.
    details: dict[str, object] = {}
    if secret_ref is not None:
        details["secret_ref"] = secret_ref
    if provider_id is not None:
        details["provider_id"] = provider_id
    if environment is not None:
        details["environment"] = environment

    event = c.AuditEvent(
        id=new_id("audit"),
        actor=actor or "system",
        action=action,
        resource_type="secret",
        resource_id=secret_id,
        details=details,
    )
    repository(request).audit_events[event.id] = event


def list_secrets(request: Request, limit: int = 50) -> c.PageResponse[c.SecretPreview]:
    if secret_repository(request) is not None:
        values = secret_repository(request).list_secrets(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository(request).secrets.values(), limit)


def create_secret(
    payload: c.CreateSecretRequest, request: Request, actor: str | None = None
) -> c.SecretPreview:
    if secret_repository(request) is not None:
        # DB backend: the repo stages the audit row in the SAME transaction as the
        # mutation (spec §32.9), so we do NOT double-write via _record_secret_audit.
        return secret_repository(request).create_secret(payload, actor=actor)
    secret = c.SecretPreview(
        id=new_id("sec"),
        provider_id=payload.provider_id,
        environment=payload.environment,
        name=payload.name,
        secret_ref=secret_store(request).put(
            payload.plaintext_secret, secret_ref=f"{new_id('sec')}.secret"
        ),
    )
    repository(request).secrets[secret.id] = secret
    _record_secret_audit(
        request,
        action="secret.create",
        secret_id=secret.id,
        secret_ref=secret.secret_ref,
        provider_id=secret.provider_id,
        environment=secret.environment,
        actor=actor,
    )
    return secret


def rotate_secret(
    secret_id: str, payload: c.RotateSecretRequest, request: Request, actor: str | None = None
) -> c.SecretPreview:
    if secret_repository(request) is not None:
        # DB backend: audit staged in the mutation transaction (spec §32.9).
        return secret_repository(request).rotate_secret(secret_id, payload, actor=actor)
    old_secret = repository(request).secrets[secret_id]
    repository(request).secrets[secret_id] = old_secret.model_copy(
        update={"status": c.SecretStatus.rotated, "rotated_at": c.utcnow(), "updated_at": c.utcnow()}
    )
    new_secret = c.SecretPreview(
        id=new_id("sec"),
        provider_id=old_secret.provider_id,
        environment=old_secret.environment,
        name=old_secret.name,
        secret_ref=secret_store(request).put(
            payload.plaintext_secret, secret_ref=f"{new_id('sec')}.secret"
        ),
        rotated_from_secret_id=old_secret.id,
    )
    repository(request).secrets[new_secret.id] = new_secret
    _record_secret_audit(
        request,
        action="secret.rotate",
        secret_id=new_secret.id,
        secret_ref=new_secret.secret_ref,
        provider_id=new_secret.provider_id,
        environment=new_secret.environment,
        actor=actor,
    )
    return new_secret


def disable_secret(
    secret_id: str, payload: c.DisableSecretRequest, request: Request, actor: str | None = None
) -> c.SecretPreview:
    if secret_repository(request) is not None:
        # DB backend: audit staged in the mutation transaction (spec §32.9).
        return secret_repository(request).disable_secret(secret_id, payload, actor=actor)
    existing = repository(request).secrets[secret_id]
    if existing.secret_ref:
        secret_store(request).disable(existing.secret_ref)
    secret = repository(request).patch(
        repository(request).secrets,
        secret_id,
        {"status": c.SecretStatus.disabled, "disabled_at": c.utcnow()},
    )
    _record_secret_audit(
        request,
        action="secret.disable",
        secret_id=secret.id,
        secret_ref=secret.secret_ref,
        provider_id=secret.provider_id,
        environment=secret.environment,
        actor=actor,
    )
    return secret
