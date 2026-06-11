from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packages.core.contracts import Artifact, ArtifactKind, ErrorCode, ProviderProfile, ProviderStatus, utcnow
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage import ObjectStore, Repository
from packages.core.storage.secret_store import SecretStore
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import probe_media


@dataclass
class ProviderInvocationContext:
    repository: Repository
    profile: ProviderProfile
    invocation_id: str
    secret_store: SecretStore | None
    object_store: ObjectStore

    def get_secret(self) -> str | None:
        if self.profile.secret_ref is None or self.secret_store is None:
            return None
        return self.secret_store.get(self.profile.secret_ref)

    def mark_polling(self, external_job_id: str) -> None:
        self.update_invocation(
            status=ProviderStatus.polling,
            updates={"external_job_id": external_job_id},
        )

    def update_invocation(self, *, status: ProviderStatus | None = None, updates: dict | None = None) -> None:
        current = self.repository.provider_invocations[self.invocation_id]
        patch = dict(updates or {})
        if status is not None:
            assert_transition("provider", current.status, status)
            patch["status"] = status
        patch["updated_at"] = utcnow()
        self.repository.provider_invocations[self.invocation_id] = current.model_copy(update=patch)

    def local_path_for_uri(self, uri: str) -> Path:
        if uri.startswith("local://"):
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
    ) -> Artifact:
        ref = self.object_store.prepare_upload(filename, purpose)
        stored = self.object_store.put_bytes(ref, content)
        media_info = probe_media(local_object_path(self.object_store, stored.ref.uri))
        return self.repository.create_artifact(
            kind=kind,
            payload_schema="uri-only",
            payload=None,
            case_id=call.case_id,
            run_id=call.run_id,
            node_run_id=call.node_run_id,
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=media_info,
        )
