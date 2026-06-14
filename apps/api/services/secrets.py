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
from packages.core.storage.database import AuditEventRow
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
    """Append a secret governance audit event (spec §11.3 / §32.9).

    Captures actor, action, secret_ref/profile and timestamp. The secret VALUE
    is never recorded. Writes to the SqlAlchemy audit table when the DB backend
    is active, otherwise into the in-memory repository's audit log so audit reads
    stay consistent across both backends.
    """
    # Spec 11.3: never log the plaintext / reversible value — only metadata.
    details: dict[str, object] = {}
    if secret_ref is not None:
        details["secret_ref"] = secret_ref
    if provider_id is not None:
        details["provider_id"] = provider_id
    if environment is not None:
        details["environment"] = environment

    repo = secret_repository(request)
    if repo is not None:
        with repo.session_factory() as session:
            session.add(
                AuditEventRow(
                    id=new_id("audit"),
                    actor=actor or "system",
                    action=action,
                    resource_type="secret",
                    resource_id=secret_id,
                    details=details,
                )
            )
            session.commit()
        return
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
        secret = secret_repository(request).create_secret(payload)
    else:
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


def read_secret(secret_id: str, request: Request, actor: str | None = None) -> str | None:
    """Reveal a secret's plaintext value for internal use, writing a ``secret.read`` audit.

    Spec §11.3: the public API never returns plaintext, but internal consumers
    (provider invocation, balance polling) read the value via the secret store.
    This seam records the read in the audit log without logging the value itself.
    Returns ``None`` if the secret or its backing value is missing.
    """
    repo = secret_repository(request)
    if repo is not None:
        secret = next((item for item in repo.list_secrets(limit=1000) if item.id == secret_id), None)
    else:
        secret = repository(request).secrets.get(secret_id)
    if secret is None or not secret.secret_ref:
        return None
    value = secret_store(request).get(secret.secret_ref)
    _record_secret_audit(
        request,
        action="secret.read",
        secret_id=secret.id,
        secret_ref=secret.secret_ref,
        provider_id=secret.provider_id,
        environment=secret.environment,
        actor=actor,
    )
    return value


def rotate_secret(
    secret_id: str, payload: c.RotateSecretRequest, request: Request, actor: str | None = None
) -> c.SecretPreview:
    if secret_repository(request) is not None:
        new_secret = secret_repository(request).rotate_secret(secret_id, payload)
    else:
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
        secret = secret_repository(request).disable_secret(secret_id, payload)
    else:
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
