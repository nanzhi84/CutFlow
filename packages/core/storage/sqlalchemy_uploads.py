from __future__ import annotations

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    MediaInfo,
    UploadSession,
    UploadStatus,
    utcnow,
)
from packages.core.storage.base_repository import BaseRepository
from packages.core.storage.database import ArtifactRow, UploadSessionRow
from packages.core.storage.repository import new_id
from packages.core.contracts.state_machines import assert_transition


def upload_row_to_contract(row: UploadSessionRow) -> UploadSession:
    return UploadSession(
        id=row.id,
        kind=row.kind,
        case_id=row.case_id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        status=UploadStatus(row.status),
        upload_url=row.object_uri,
        local_temp_path=row.local_temp_path,
        object_uri=row.object_uri,
        stabilize=row.stabilize,
        stabilized=row.stabilized,
        expires_at=row.expires_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def artifact_row_to_contract(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        case_id=row.case_id,
        run_id=row.run_id,
        node_run_id=row.node_run_id,
        kind=ArtifactKind(row.kind),
        uri=row.uri,
        local_path=row.local_path,
        oss_uri=row.oss_uri,
        size_bytes=row.size_bytes,
        immutable=row.immutable,
        retention_policy=row.retention_policy,
        sha256=row.sha256,
        media_info=row.media_info,
        payload_schema=row.payload_schema,
        payload=row.payload,
        created_by_node_run_id=row.created_by_node_run_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def artifact_to_row(artifact: Artifact) -> ArtifactRow:
    return ArtifactRow(
        id=artifact.id,
        case_id=artifact.case_id,
        run_id=artifact.run_id,
        node_run_id=artifact.node_run_id,
        kind=artifact.kind.value,
        uri=artifact.uri,
        local_path=artifact.local_path,
        oss_uri=artifact.oss_uri,
        size_bytes=artifact.size_bytes,
        immutable=artifact.immutable,
        retention_policy=artifact.retention_policy,
        sha256=artifact.sha256,
        media_info=artifact.media_info.model_dump(mode="json") if artifact.media_info else None,
        payload_schema=artifact.payload_schema,
        payload=artifact.payload,
        created_by_node_run_id=artifact.created_by_node_run_id,
        schema_version=artifact.schema_version,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
    )


class SqlAlchemyUploadRepository(BaseRepository):

    def create_upload(self, upload: UploadSession) -> UploadSession:
        with self.session_factory() as session:
            row = UploadSessionRow(
                id=upload.id,
                kind=upload.kind.value,
                case_id=upload.case_id,
                filename=upload.filename,
                content_type=upload.content_type,
                size_bytes=upload.size_bytes,
                sha256=upload.sha256,
                status=upload.status.value,
                object_uri=upload.object_uri,
                local_temp_path=upload.local_temp_path,
                stabilize=upload.stabilize,
                stabilized=upload.stabilized,
                expires_at=upload.expires_at,
                schema_version=upload.schema_version,
                created_at=upload.created_at,
                updated_at=upload.updated_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return upload_row_to_contract(row)

    def get_upload(self, upload_session_id: str) -> UploadSession | None:
        with self.session_factory() as session:
            row = session.get(UploadSessionRow, upload_session_id)
            return upload_row_to_contract(row) if row else None

    def patch_upload(self, upload_session_id: str, updates: dict) -> UploadSession:
        with self.session_factory() as session:
            row = session.get(UploadSessionRow, upload_session_id)
            if row is None:
                raise KeyError(upload_session_id)
            for key, value in updates.items():
                if key == "status" and isinstance(value, UploadStatus):
                    value = value.value
                if key == "status":
                    assert_transition("upload_session", row.status, value)
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return upload_row_to_contract(row)

    def create_artifact_from_upload(
        self, upload: UploadSession, *, media_info: MediaInfo | None = None
    ) -> Artifact:
        return self.create_artifact(
            kind=ArtifactKind.uploaded_file,
            uri=upload.object_uri,
            size_bytes=upload.size_bytes,
            sha256=upload.sha256,
            media_info=media_info,
            payload_schema="UploadedFileArtifact.v1",
            payload=upload.model_dump(mode="json"),
        )

    def create_artifact(
        self,
        *,
        kind: ArtifactKind,
        payload_schema: str,
        payload,
        uri: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        media_info: MediaInfo | None = None,
    ) -> Artifact:
        with self.session_factory() as session:
            row = ArtifactRow(
                id=new_id("art"),
                kind=kind.value,
                uri=uri,
                size_bytes=size_bytes,
                sha256=sha256,
                media_info=media_info.model_dump(mode="json") if media_info else None,
                payload_schema=payload_schema,
                payload=payload,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return artifact_row_to_contract(row)

    def artifact_ref(self, artifact_id: str) -> ArtifactRef:
        with self.session_factory() as session:
            row = session.get(ArtifactRow, artifact_id)
            if row is None:
                raise KeyError(artifact_id)
            return ArtifactRef(
                artifact_id=row.id,
                kind=ArtifactKind(row.kind),
                uri=row.uri or f"artifact://{row.id}",
                schema_version=row.schema_version,
                sha256=row.sha256,
            )
