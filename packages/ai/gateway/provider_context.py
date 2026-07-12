from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.core.contracts import Artifact, ArtifactKind, ErrorCode, ProviderProfile, ProviderStatus, utcnow
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage import ObjectStore, Repository
from packages.core.storage.secret_store import SecretStore
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import probe_media

logger = logging.getLogger(__name__)


@dataclass
class ProviderInvocationContext:
    repository: Repository
    profile: ProviderProfile
    invocation_id: str
    secret_store: SecretStore | None
    object_store: ObjectStore
    # Optional durable audit sink (a callable that PERSISTS an audit event to the
    # backing store, e.g. the SQL audit table). When None — the in-memory backend —
    # the read audit is recorded onto self.repository.audit_events instead, so a
    # secret.read is auditable in both deployments. Wired by the ProviderGateway.
    audit_sink: Callable[..., Any] | None = None
    # Durable invocation store for Run-scoped idempotent calls. When set, mark_polling
    # commits external_job_id to PostgreSQL the moment the async task is accepted, so a
    # crash after acceptance recovers the task instead of re-submitting. None on the
    # in-memory/transient path, where only the run-state repository is updated.
    durable_invocation_store: Any | None = None

    def get_secret(self) -> str | None:
        if self.profile.secret_ref is None or self.secret_store is None:
            return None
        value = self.secret_store.get(self.profile.secret_ref)
        # This is the LIVE secret-value reveal on the provider hot path (spec §11.3).
        # Spec §32.9 mandates fail-closed audit for governance ops, but a reveal here
        # happens inside a vendor-bound provider call in a worker process; failing the
        # call on an audit hiccup would trade availability for strictness. We therefore
        # INTENTIONALLY relax §32.9 to best-effort (log + swallow) for this read path
        # only — the secret VALUE is never recorded, just access metadata.
        if value is not None:
            self._audit_secret_read()
        return value

    def _audit_secret_read(self) -> None:
        actor = self.profile.provider_id or "provider-gateway"
        resource_id = self.profile.secret_ref
        details = {
            "secret_ref": self.profile.secret_ref,
            "provider_id": self.profile.provider_id,
            "environment": self.profile.environment,
        }
        try:
            if self.audit_sink is not None:
                # Durable persist (e.g. SQL audit table) so a worker-process reveal
                # is recorded even though it never touches the in-memory repository.
                self.audit_sink(
                    actor=actor,
                    action="secret.read",
                    resource_type="secret",
                    resource_id=resource_id,
                    details=details,
                )
                return
            # In-memory backend: record onto the runtime repository's audit log so
            # the read is still auditable (same store ops.audit_events reads from).
            from packages.core import contracts as c
            from packages.core.storage.repository import new_id

            event = c.AuditEvent(
                id=new_id("audit"),
                actor=actor,
                action="secret.read",
                resource_type="secret",
                resource_id=resource_id,
                details=details,
            )
            self.repository.audit_events[event.id] = event
        except Exception:
            logger.warning(
                "secret.read audit failed for provider_id=%s secret_ref=%s (continuing)",
                self.profile.provider_id,
                self.profile.secret_ref,
                exc_info=True,
            )

    def mark_polling(self, external_job_id: str) -> None:
        self.update_invocation(
            status=ProviderStatus.polling,
            updates={"external_job_id": external_job_id},
        )
        if self.durable_invocation_store is not None:
            self.durable_invocation_store.mark_polling(self.invocation_id, external_job_id)

    def update_invocation(self, *, status: ProviderStatus | None = None, updates: dict | None = None) -> None:
        current = self.repository.provider_invocations[self.invocation_id]
        patch = dict(updates or {})
        if status is not None:
            assert_transition("provider", current.status, status)
            patch["status"] = status
        patch["updated_at"] = utcnow()
        self.repository.provider_invocations[self.invocation_id] = current.model_copy(update=patch)

    def local_path_for_uri(self, uri: str) -> Path:
        if uri.startswith(("local://", "s3://")):
            return local_object_path(self.object_store, uri)
        path = Path(uri)
        if path.exists():
            return path
        from packages.ai.gateway.provider_gateway import ProviderRuntimeError

        raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, f"Unsupported media URI: {uri}")

    def store_media_bytes(
        self,
        *,
        content: bytes,
        filename: str,
        purpose: str,
        kind: ArtifactKind,
        call,
        tier: str = "durable",
    ) -> Artifact:
        ref = self.object_store.prepare_upload(filename, purpose, tier=tier)
        stored = self.object_store.put_bytes(ref, content)
        self._ensure_stored_object_exists(stored.ref.uri)
        media_info = probe_media(local_object_path(self.object_store, stored.ref.uri))
        return self.repository.create_artifact(
            kind=kind,
            payload_schema="uri-only",
            payload=None,
            case_id=call.case_id,
            run_id=call.run_id,
            node_run_id=call.node_run_id,
            uri=stored.ref.uri,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            media_info=media_info,
        )

    def store_media_file(
        self,
        *,
        local_path: Path,
        filename: str,
        purpose: str,
        kind: ArtifactKind,
        call,
        tier: str = "durable",
    ) -> Artifact:
        source = Path(local_path)
        ref = self.object_store.prepare_upload(filename, purpose, tier=tier)
        stored = self.object_store.upload_file(source, ref)
        self._ensure_stored_object_exists(stored.ref.uri)
        media_info = probe_media(source)
        return self.repository.create_artifact(
            kind=kind,
            payload_schema="uri-only",
            payload=None,
            case_id=call.case_id,
            run_id=call.run_id,
            node_run_id=call.node_run_id,
            uri=stored.ref.uri,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            media_info=media_info,
        )

    def _ensure_stored_object_exists(self, uri: str) -> None:
        from packages.ai.gateway.provider_gateway import ProviderRuntimeError
        from packages.core.storage.object_store import parse_object_uri

        try:
            ref = parse_object_uri(uri)
            exists = self.object_store.exists(ref)
        except Exception as exc:
            raise ProviderRuntimeError(
                ErrorCode.artifact_missing,
                f"Stored media object is not readable after upload: {uri}",
            ) from exc
        if not exists:
            raise ProviderRuntimeError(
                ErrorCode.artifact_missing,
                f"Stored media object is missing after upload: {uri}",
            )
