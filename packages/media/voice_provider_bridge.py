from __future__ import annotations

from packages.core import contracts as c
from packages.core.storage.database import ArtifactRow, UploadSessionRow, VoiceProfileRow
from packages.core.storage.repository import Repository
from packages.core.storage.sqlalchemy_uploads import artifact_to_row, upload_row_to_contract
from packages.core.workflow import NodeExecutionError
from packages.media.sqlalchemy_repository import artifact_ref_from_row, voice_row_to_contract


def load_voice(media_repository, voice_id: str) -> c.VoiceProfile | None:
    getter = getattr(media_repository, "get_voice", None)
    if callable(getter):
        return getter(voice_id)
    with media_repository.session_factory() as session:
        row = session.get(VoiceProfileRow, voice_id)
        return voice_row_to_contract(row) if row is not None else None


def hydrate_voice_reference_upload(media_repository, repository: Repository, upload_id: str) -> None:
    hydrator = getattr(media_repository, "hydrate_voice_reference_upload", None)
    if callable(hydrator):
        hydrator(repository, upload_id)
        return
    with media_repository.session_factory() as session:
        row = session.get(UploadSessionRow, upload_id)
        if row is None or row.status != c.UploadStatus.completed.value:
            raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Reference upload must be completed first.")
        upload = upload_row_to_contract(row)
        repository.uploads[upload.id] = upload


def persist_provider_voice(media_repository, voice: c.VoiceProfile) -> c.VoiceProfile:
    persister = getattr(media_repository, "persist_provider_voice", None)
    if callable(persister):
        return persister(voice)
    with media_repository.session_factory() as session:
        row = VoiceProfileRow(
            id=voice.id,
            display_name=voice.display_name,
            source=voice.source,
            vendor=voice.vendor,
            provider_profile_id=voice.provider_profile_id,
            preview_artifact_id=voice.preview_artifact_id,
            enabled=voice.enabled,
            status=voice.status,
            schema_version=voice.schema_version,
            created_at=voice.created_at,
            updated_at=voice.updated_at,
        )
        merged = session.merge(row)
        session.commit()
        session.refresh(merged)
        return voice_row_to_contract(merged)


def persist_provider_preview(media_repository, voice_id: str, artifact: c.Artifact) -> c.ArtifactRef:
    updater = getattr(media_repository, "update_provider_preview", None)
    if callable(updater):
        return updater(voice_id, artifact)
    with media_repository.session_factory() as session:
        row = session.get(ArtifactRow, artifact.id)
        if row is None:
            row = artifact_to_row(artifact)
            session.add(row)
        voice = session.get(VoiceProfileRow, voice_id)
        if voice is None:
            raise NodeExecutionError(c.ErrorCode.validation_missing_voice, "Voice not found.")
        voice.preview_artifact_id = artifact.id
        voice.updated_at = c.utcnow()
        session.commit()
        session.refresh(row)
        return artifact_ref_from_row(row)
