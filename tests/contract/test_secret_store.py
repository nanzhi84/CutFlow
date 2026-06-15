
from fastapi import FastAPI
from starlette.requests import Request

from apps.api.app import configure_app_state
from apps.api.routers import secrets as secrets_router
from apps.api.services import secrets as service
from packages.core import contracts as c
from packages.core.storage.secret_store import LocalSecretStore


def test_local_secret_store_writes_0600_file_and_disables_secret(tmp_path):
    store = LocalSecretStore(root=tmp_path)

    secret_ref = store.put("plain-secret")

    secret_path = tmp_path / secret_ref
    assert secret_path.exists()
    assert secret_path.stat().st_mode & 0o777 == 0o600
    assert "plain-secret" not in secret_path.read_text(encoding="utf-8")
    assert store.get(secret_ref) == "plain-secret"

    store.disable(secret_ref)

    assert not secret_path.exists()
    assert store.get(secret_ref) is None


def _memory_app() -> FastAPI:
    # Build in-memory app state directly (no lifespan / no background dispatcher),
    # which keeps the secret-store + audit pipeline reachable via apps.api.common.
    app = FastAPI()
    configure_app_state(app, session_factory=None)
    return app


def _request(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


def _secret_audit_events(request: Request) -> list[c.AuditEvent]:
    from apps.api.common import repository

    return [
        event
        for event in repository(request).audit_events.values()
        if getattr(event, "resource_type", None) == "secret"
    ]


def test_secret_create_rotate_disable_write_audit_events_without_plaintext():
    # Spec 11.3 / 32.9: create/rotate/disable secret operations all write audit,
    # capturing actor + action + secret metadata but never the secret value.
    plaintext = "super-secret-value-xyz"
    rotated_plaintext = "rotated-secret-value-abc"
    app = _memory_app()
    request = _request(app)

    created = service.create_secret(
        c.CreateSecretRequest(
            provider_id="sandbox-audit",
            environment="local",
            name="Audit key",
            plaintext_secret=plaintext,
        ),
        request,
        actor="usr_admin",
    )
    rotated = service.rotate_secret(
        created.id,
        c.RotateSecretRequest(plaintext_secret=rotated_plaintext, reason="rotate audit"),
        request,
        actor="usr_admin",
    )
    service.disable_secret(
        rotated.id,
        c.DisableSecretRequest(reason="disable audit"),
        request,
        actor="usr_admin",
    )

    events = _secret_audit_events(request)
    by_action = {event.action: event for event in events}

    assert "secret.create" in by_action
    assert "secret.rotate" in by_action
    assert "secret.disable" in by_action

    create_event = by_action["secret.create"]
    assert create_event.resource_id == created.id
    assert create_event.actor == "usr_admin"
    assert create_event.details["provider_id"] == "sandbox-audit"
    assert create_event.details["environment"] == "local"
    assert create_event.details["secret_ref"] == created.secret_ref

    rotate_event = by_action["secret.rotate"]
    assert rotate_event.resource_id == rotated.id

    disable_event = by_action["secret.disable"]
    assert disable_event.resource_id == rotated.id

    # No audit entry may ever carry the plaintext value.
    serialized = repr([(event.action, event.actor, event.details) for event in events])
    assert plaintext not in serialized
    assert rotated_plaintext not in serialized


def test_router_passes_authenticated_actor_into_audit():
    # The router captures require_role's AuthUser and forwards user.id as actor,
    # so audit events attribute the real admin rather than the bare "system".
    import inspect

    source = inspect.getsource(secrets_router)
    assert "actor=user.id" in source
