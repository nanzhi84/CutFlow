from __future__ import annotations

from datetime import timedelta
from collections.abc import Callable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ErrorCode,
    MediaAssetRecord,
    MediaInfo,
    PublishDefaults,
    PublishPackage,
    UploadKind,
    UploadSession,
    UploadStatus,
    UploadStrategy,
    normalize_visual_asset_kind,
    utcnow,
)
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage.base_repository import BaseRepository
from packages.core.storage.database import (
    ArtifactRow,
    MediaAssetRow,
    PublishPackageRow,
    UploadSessionRow,
)
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError

_RECONCILABLE_STATUSES = (
    UploadStatus.completing.value,
    UploadStatus.object_completed.value,
    UploadStatus.verified.value,
)
_IMMUTABLE_TERMINAL_STATUSES = {
    UploadStatus.ready.value,
    UploadStatus.rejected.value,
    UploadStatus.failed.value,
    UploadStatus.cancelled.value,
    UploadStatus.expired.value,
}


def upload_row_to_contract(row: UploadSessionRow) -> UploadSession:
    media_info = (
        MediaInfo.model_validate(row.verified_media_info)
        if isinstance(row.verified_media_info, dict)
        else None
    )
    return UploadSession(
        id=row.id,
        client_upload_id=row.client_upload_id,
        owner_user_id=row.owner_user_id,
        kind=row.kind,
        case_id=row.case_id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        final_size_bytes=row.final_size_bytes,
        sha256=row.sha256,
        client_expected_sha256=row.client_expected_sha256,
        canonical_sha256=row.canonical_sha256,
        status=UploadStatus(row.status),
        upload_strategy=UploadStrategy(row.upload_strategy),
        part_size_bytes=row.part_size_bytes,
        part_count=row.part_count,
        upload_url=row.object_uri,
        local_temp_path=row.local_temp_path,
        object_uri=row.object_uri,
        staging_uri=row.staging_uri,
        final_uri=row.final_uri,
        stabilize=row.stabilize,
        stabilized=row.stabilized,
        normalized=row.normalized,
        completion_metadata=dict(row.completion_metadata or {}),
        verified_media_info=media_info,
        last_error=row.last_error,
        retry_count=row.retry_count,
        next_retry_at=row.next_retry_at,
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
        source_upload_session_id=row.source_upload_session_id,
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
        source_upload_session_id=artifact.source_upload_session_id,
        schema_version=artifact.schema_version,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
    )


def _artifact_ref(row: ArtifactRow) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=row.id,
        kind=ArtifactKind(row.kind),
        uri=row.uri or f"artifact://{row.id}",
        schema_version=row.schema_version,
        sha256=row.sha256,
    )


def _media_asset_contract(row: MediaAssetRow) -> MediaAssetRecord:
    return MediaAssetRecord(
        id=row.id,
        case_id=row.case_id,
        title=row.title,
        kind=row.kind,
        source_artifact_id=row.source_artifact_id,
        tags=list(row.tags or []),
        annotation_status=row.annotation_status,
        usable=row.usable,
        thumbnail_url=None,
        duration_sec=row.duration_sec,
        width=row.width,
        height=row.height,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _publish_package_contract(row: PublishPackageRow) -> PublishPackage:
    return PublishPackage(
        id=row.id,
        case_id=row.case_id,
        source_finished_video_id=row.source_finished_video_id,
        upload_artifact_id=row.upload_artifact_id,
        video_artifact=ArtifactRef.model_validate(row.video_artifact),
        cover_artifact=(
            ArtifactRef.model_validate(row.cover_artifact) if row.cover_artifact else None
        ),
        platform_defaults=PublishDefaults.model_validate(row.platform_defaults),
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyUploadRepository(BaseRepository):
    def create_upload(
        self, upload: UploadSession, *, multipart_upload_id: str | None = None
    ) -> UploadSession:
        """Insert a session or return the concurrent idempotent prepare winner."""

        with self.session_factory() as session:
            row = UploadSessionRow(
                id=upload.id,
                client_upload_id=upload.client_upload_id,
                owner_user_id=upload.owner_user_id,
                kind=upload.kind.value,
                case_id=upload.case_id,
                filename=upload.filename,
                content_type=upload.content_type,
                size_bytes=upload.size_bytes,
                final_size_bytes=upload.final_size_bytes,
                sha256=upload.sha256,
                client_expected_sha256=upload.client_expected_sha256,
                canonical_sha256=upload.canonical_sha256,
                status=upload.status.value,
                upload_strategy=upload.upload_strategy.value,
                multipart_upload_id=multipart_upload_id,
                part_size_bytes=upload.part_size_bytes,
                part_count=upload.part_count,
                object_uri=upload.object_uri,
                staging_uri=upload.staging_uri,
                final_uri=upload.final_uri,
                local_temp_path=upload.local_temp_path,
                stabilize=upload.stabilize,
                stabilized=upload.stabilized,
                normalized=upload.normalized,
                completion_metadata=upload.completion_metadata,
                verified_media_info=(
                    upload.verified_media_info.model_dump(mode="json")
                    if upload.verified_media_info
                    else None
                ),
                last_error=upload.last_error,
                retry_count=upload.retry_count,
                next_retry_at=upload.next_retry_at,
                expires_at=upload.expires_at,
                schema_version=upload.schema_version,
                created_at=upload.created_at,
                updated_at=upload.updated_at,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(UploadSessionRow).where(
                        UploadSessionRow.client_upload_id == upload.client_upload_id
                    )
                )
                if existing is None:
                    raise
                self._assert_same_prepare(existing, upload)
                return upload_row_to_contract(existing)
            session.refresh(row)
            return upload_row_to_contract(row)

    @staticmethod
    def _assert_same_prepare(row: UploadSessionRow, upload: UploadSession) -> None:
        identity = (
            row.owner_user_id,
            row.kind,
            row.case_id,
            row.filename,
            row.content_type,
            row.size_bytes,
            row.client_expected_sha256,
            row.stabilize,
        )
        requested = (
            upload.owner_user_id,
            upload.kind.value,
            upload.case_id,
            upload.filename,
            upload.content_type,
            upload.size_bytes,
            upload.client_expected_sha256,
            upload.stabilize,
        )
        if identity != requested:
            raise NodeExecutionError(
                ErrorCode.idempotency_conflict,
                "client_upload_id was already used for a different upload.",
            )

    def get_upload(self, upload_session_id: str) -> UploadSession | None:
        with self.session_factory() as session:
            row = session.get(UploadSessionRow, upload_session_id)
            return upload_row_to_contract(row) if row else None

    def get_upload_by_client_id(self, client_upload_id: str) -> UploadSession | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(UploadSessionRow).where(
                    UploadSessionRow.client_upload_id == client_upload_id
                )
            )
            return upload_row_to_contract(row) if row else None

    def multipart_upload_id(self, upload_session_id: str) -> str | None:
        with self.session_factory() as session:
            row = session.get(UploadSessionRow, upload_session_id)
            return row.multipart_upload_id if row else None

    def ensure_multipart_upload_id(self, upload_session_id: str, create: Callable[[], str]) -> str:
        """Create the remote multipart id once while serializing prepare retries."""

        with self.session_factory() as session:
            row = session.scalar(
                select(UploadSessionRow)
                .where(UploadSessionRow.id == upload_session_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(upload_session_id)
            if row.multipart_upload_id:
                return row.multipart_upload_id
            upload_id = create()
            row.multipart_upload_id = upload_id
            row.updated_at = utcnow()
            session.commit()
            return upload_id

    def patch_upload(self, upload_session_id: str, updates: dict) -> UploadSession:
        with self.session_factory() as session:
            # State transitions can race with cancel/expiry or another reconciler.
            # Lock and re-read the latest row so a stale transition can never
            # overwrite a terminal state after validating an older snapshot.
            row = session.scalar(
                select(UploadSessionRow)
                .where(UploadSessionRow.id == upload_session_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(upload_session_id)
            self._apply_updates(row, updates)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return upload_row_to_contract(row)

    @staticmethod
    def _apply_updates(row: UploadSessionRow, updates: dict) -> None:
        for key, value in updates.items():
            if key == "status" and isinstance(value, UploadStatus):
                value = value.value
            if key == "upload_strategy" and isinstance(value, UploadStrategy):
                value = value.value
            if key == "verified_media_info" and isinstance(value, MediaInfo):
                value = value.model_dump(mode="json")
            if key == "status" and row.status != value:
                assert_transition("upload_session", row.status, value)
            setattr(row, key, value)

    def mark_completing(
        self,
        upload_session_id: str,
        *,
        size_bytes: int,
        expected_sha256: str | None,
        metadata: dict[str, str],
    ) -> UploadSession:
        with self.session_factory() as session:
            row = session.scalar(
                select(UploadSessionRow)
                .where(UploadSessionRow.id == upload_session_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(upload_session_id)
            if row.size_bytes != size_bytes:
                raise NodeExecutionError(ErrorCode.upload_size_mismatch, "Upload size mismatch.")
            if (
                expected_sha256
                and row.client_expected_sha256
                and row.client_expected_sha256 != expected_sha256
            ):
                raise NodeExecutionError(
                    ErrorCode.upload_sha256_mismatch,
                    "Upload sha256 differs from the value used during prepare.",
                )
            if row.status in {
                UploadStatus.completing.value,
                UploadStatus.object_completed.value,
                UploadStatus.verified.value,
                UploadStatus.ready.value,
            }:
                if row.completion_metadata != metadata:
                    raise NodeExecutionError(
                        ErrorCode.idempotency_conflict,
                        "Upload completion metadata differs from the first request.",
                    )
                if expected_sha256 and row.client_expected_sha256 is None:
                    row.client_expected_sha256 = expected_sha256
                    row.updated_at = utcnow()
                    session.commit()
                    session.refresh(row)
                return upload_row_to_contract(row)
            if row.status not in {UploadStatus.prepared.value, UploadStatus.uploading.value}:
                raise NodeExecutionError(
                    ErrorCode.upload_invalid_state,
                    f"Upload cannot complete from status {row.status}.",
                )
            assert_transition("upload_session", row.status, UploadStatus.completing.value)
            row.status = UploadStatus.completing.value
            if expected_sha256:
                row.client_expected_sha256 = expected_sha256
            row.completion_metadata = dict(metadata)
            row.last_error = None
            row.next_retry_at = None
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return upload_row_to_contract(row)

    def claim_reconcilable(
        self, *, owner: str, limit: int, lease_seconds: int
    ) -> list[UploadSession]:
        now = utcnow()
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(UploadSessionRow)
                    .where(UploadSessionRow.status.in_(_RECONCILABLE_STATUSES))
                    .where(
                        or_(
                            UploadSessionRow.next_retry_at.is_(None),
                            UploadSessionRow.next_retry_at <= now,
                        )
                    )
                    .where(
                        or_(
                            UploadSessionRow.lease_expires_at.is_(None),
                            UploadSessionRow.lease_expires_at <= now,
                        )
                    )
                    .order_by(UploadSessionRow.updated_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.lease_owner = owner
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            session.commit()
            return [upload_row_to_contract(row) for row in rows]

    def claim_ready_derivations(
        self, *, owner: str, limit: int, lease_seconds: int
    ) -> list[UploadSession]:
        """Lease ready visual uploads whose MediaAsset still lacks a thumbnail."""

        now = utcnow()
        missing_thumbnail = (
            select(MediaAssetRow.id)
            .join(ArtifactRow, MediaAssetRow.source_artifact_id == ArtifactRow.id)
            .where(ArtifactRow.source_upload_session_id == UploadSessionRow.id)
            .where(MediaAssetRow.thumbnail_uri.is_(None))
            .exists()
        )
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(UploadSessionRow)
                    .where(UploadSessionRow.status == UploadStatus.ready.value)
                    .where(
                        UploadSessionRow.kind.in_(
                            (
                                UploadKind.video.value,
                                UploadKind.image.value,
                                UploadKind.cover_template.value,
                            )
                        )
                    )
                    .where(missing_thumbnail)
                    .where(
                        or_(
                            UploadSessionRow.next_retry_at.is_(None),
                            UploadSessionRow.next_retry_at <= now,
                        )
                    )
                    .where(
                        or_(
                            UploadSessionRow.lease_expires_at.is_(None),
                            UploadSessionRow.lease_expires_at <= now,
                        )
                    )
                    .order_by(UploadSessionRow.updated_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.lease_owner = owner
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            session.commit()
            return [upload_row_to_contract(row) for row in rows]

    def expire_stale_uploads(self, *, limit: int = 100) -> list[UploadSession]:
        now = utcnow()
        expirable = (
            UploadStatus.prepared.value,
            UploadStatus.uploading.value,
            UploadStatus.completing.value,
            UploadStatus.object_completed.value,
            UploadStatus.verified.value,
        )
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(UploadSessionRow)
                    .where(UploadSessionRow.status.in_(expirable))
                    .where(UploadSessionRow.expires_at <= now)
                    .where(
                        or_(
                            UploadSessionRow.lease_expires_at.is_(None),
                            UploadSessionRow.lease_expires_at <= now,
                        )
                    )
                    .order_by(UploadSessionRow.expires_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                assert_transition("upload_session", row.status, UploadStatus.expired.value)
                row.status = UploadStatus.expired.value
                row.last_error = "Upload session expired before it became ready."
                row.lease_owner = None
                row.lease_expires_at = None
                row.next_retry_at = None
                row.updated_at = now
            session.commit()
            return [upload_row_to_contract(row) for row in rows]

    def clear_lease(self, upload_session_id: str) -> None:
        with self.session_factory() as session:
            row = session.get(UploadSessionRow, upload_session_id)
            if row is None:
                return
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utcnow()
            session.commit()

    def record_retry(
        self, upload_session_id: str, error: str, *, max_retries: int
    ) -> UploadSession:
        with self.session_factory() as session:
            row = session.scalar(
                select(UploadSessionRow)
                .where(UploadSessionRow.id == upload_session_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(upload_session_id)
            if row.status in _IMMUTABLE_TERMINAL_STATUSES:
                row.lease_owner = None
                row.lease_expires_at = None
                session.commit()
                session.refresh(row)
                return upload_row_to_contract(row)
            row.retry_count += 1
            row.last_error = error[:4000]
            row.lease_owner = None
            row.lease_expires_at = None
            if row.retry_count >= max_retries:
                if row.status != UploadStatus.failed.value:
                    assert_transition("upload_session", row.status, UploadStatus.failed.value)
                row.status = UploadStatus.failed.value
                row.next_retry_at = None
            else:
                backoff_seconds = min(300, 2 ** min(row.retry_count, 8))
                row.next_retry_at = utcnow() + timedelta(seconds=backoff_seconds)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return upload_row_to_contract(row)

    def record_derivation_retry(self, upload_session_id: str, error: str) -> UploadSession:
        """Back off a post-ready derivative without rolling the upload out of ready."""

        with self.session_factory() as session:
            row = session.scalar(
                select(UploadSessionRow)
                .where(UploadSessionRow.id == upload_session_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(upload_session_id)
            if row.status != UploadStatus.ready.value:
                raise NodeExecutionError(
                    ErrorCode.upload_invalid_state,
                    f"Derivative retry requires ready, got {row.status}.",
                )
            row.retry_count += 1
            row.last_error = error[:4000]
            row.next_retry_at = utcnow() + timedelta(seconds=min(300, 2 ** min(row.retry_count, 8)))
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return upload_row_to_contract(row)

    def mark_derivation_complete(self, upload_session_id: str) -> None:
        with self.session_factory() as session:
            row = session.get(UploadSessionRow, upload_session_id)
            if row is None:
                return
            row.retry_count = 0
            row.last_error = None
            row.next_retry_at = None
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utcnow()
            session.commit()

    def reject_upload(self, upload_session_id: str, reason: str) -> UploadSession:
        with self.session_factory() as session:
            row = session.scalar(
                select(UploadSessionRow)
                .where(UploadSessionRow.id == upload_session_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(upload_session_id)
            if row.status in _IMMUTABLE_TERMINAL_STATUSES:
                row.lease_owner = None
                row.lease_expires_at = None
                session.commit()
                session.refresh(row)
                return upload_row_to_contract(row)
            if row.status != UploadStatus.rejected.value:
                assert_transition("upload_session", row.status, UploadStatus.rejected.value)
                row.status = UploadStatus.rejected.value
            row.last_error = reason[:4000]
            row.next_retry_at = None
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return upload_row_to_contract(row)

    def finalize_ready(
        self, upload_session_id: str
    ) -> tuple[UploadSession, ArtifactRef, MediaAssetRecord | None, PublishPackage | None]:
        """Atomically get/create upload resources and move verified -> ready."""

        with self.session_factory() as session:
            row = session.scalar(
                select(UploadSessionRow)
                .where(UploadSessionRow.id == upload_session_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(upload_session_id)
            if row.status == UploadStatus.ready.value:
                return self._resources_from_session(session, row)
            if row.status != UploadStatus.verified.value:
                raise NodeExecutionError(
                    ErrorCode.upload_invalid_state,
                    f"Upload must be verified before registration, got {row.status}.",
                )

            artifact = session.scalar(
                select(ArtifactRow).where(ArtifactRow.source_upload_session_id == upload_session_id)
            )
            if artifact is None:
                ready_contract = upload_row_to_contract(row).model_copy(
                    update={"status": UploadStatus.ready}
                )
                artifact = ArtifactRow(
                    id=new_id("art"),
                    case_id=row.case_id,
                    kind=ArtifactKind.uploaded_file.value,
                    uri=row.final_uri or row.object_uri,
                    size_bytes=row.final_size_bytes or row.size_bytes,
                    sha256=row.sha256 or row.canonical_sha256,
                    media_info=row.verified_media_info,
                    payload_schema="UploadedFileArtifact.v1",
                    payload=ready_contract.model_dump(mode="json"),
                    source_upload_session_id=row.id,
                )
                session.add(artifact)
                session.flush()

            media_asset = self._get_or_create_media_asset(session, row, artifact)
            publish_package = self._get_or_create_publish_package(session, row, artifact)
            assert_transition("upload_session", row.status, UploadStatus.ready.value)
            row.status = UploadStatus.ready.value
            row.object_uri = row.final_uri or row.object_uri
            row.lease_owner = None
            row.lease_expires_at = None
            row.next_retry_at = None
            row.last_error = None
            row.updated_at = utcnow()
            try:
                session.commit()
            except IntegrityError:
                # A non-cooperating concurrent writer may win a unique constraint.
                # Treat it as the idempotent winner and read the canonical result.
                session.rollback()
                return self.ready_resources(upload_session_id)
            session.refresh(row)
            session.refresh(artifact)
            if media_asset is not None:
                session.refresh(media_asset)
            if publish_package is not None:
                session.refresh(publish_package)
            return (
                upload_row_to_contract(row),
                _artifact_ref(artifact),
                _media_asset_contract(media_asset) if media_asset is not None else None,
                _publish_package_contract(publish_package) if publish_package is not None else None,
            )

    @staticmethod
    def _get_or_create_media_asset(
        session, upload: UploadSessionRow, artifact: ArtifactRow
    ) -> MediaAssetRow | None:
        replace_mode = (upload.completion_metadata or {}).get("template_mode") == "replace"
        if replace_mode or upload.kind not in {
            UploadKind.video.value,
            UploadKind.image.value,
            UploadKind.bgm.value,
            UploadKind.font.value,
            UploadKind.cover_template.value,
        }:
            return None
        existing = session.scalar(
            select(MediaAssetRow).where(MediaAssetRow.source_artifact_id == artifact.id)
        )
        if existing is not None:
            return existing
        persisted_kind, legacy_tag = normalize_visual_asset_kind(upload.kind)
        tags = [persisted_kind, "upload"]
        if legacy_tag:
            tags.append(legacy_tag)
        if upload.stabilized:
            tags.append("stabilized")
        metadata = dict(upload.completion_metadata or {})
        if upload.normalized or metadata.get("normalized") == "1":
            tags.append("normalized")
        if metadata.get("ai_material") == "1":
            tags.append("ai_material")
        media_info = upload.verified_media_info or {}
        row = MediaAssetRow(
            id=new_id("asset"),
            case_id=upload.case_id,
            title=metadata.get("title") or upload.filename,
            kind=persisted_kind,
            source_artifact_id=artifact.id,
            tags=tags,
            annotation_status="pending",
            usable=True,
            thumbnail_uri=None,
            duration_sec=media_info.get("duration_sec"),
            width=media_info.get("width"),
            height=media_info.get("height"),
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _get_or_create_publish_package(
        session, upload: UploadSessionRow, artifact: ArtifactRow
    ) -> PublishPackageRow | None:
        if upload.kind != UploadKind.publish_video.value:
            return None
        existing = session.scalar(
            select(PublishPackageRow).where(PublishPackageRow.upload_artifact_id == artifact.id)
        )
        if existing is not None:
            return existing
        metadata = dict(upload.completion_metadata or {})
        row = PublishPackageRow(
            id=new_id("pkg"),
            case_id=upload.case_id,
            source_finished_video_id=None,
            upload_artifact_id=artifact.id,
            video_artifact=_artifact_ref(artifact).model_dump(mode="json"),
            cover_artifact=None,
            platform_defaults=PublishDefaults(
                title=metadata.get("title") or upload.filename,
                description=metadata.get("description", ""),
            ).model_dump(mode="json"),
        )
        session.add(row)
        session.flush()
        return row

    def ready_resources(
        self, upload_session_id: str
    ) -> tuple[UploadSession, ArtifactRef, MediaAssetRecord | None, PublishPackage | None]:
        with self.session_factory() as session:
            upload = session.get(UploadSessionRow, upload_session_id)
            if upload is None:
                raise KeyError(upload_session_id)
            return self._resources_from_session(session, upload)

    @staticmethod
    def _resources_from_session(
        session, upload: UploadSessionRow
    ) -> tuple[UploadSession, ArtifactRef, MediaAssetRecord | None, PublishPackage | None]:
        artifact = session.scalar(
            select(ArtifactRow).where(ArtifactRow.source_upload_session_id == upload.id)
        )
        if artifact is None:
            raise NodeExecutionError(
                ErrorCode.artifact_missing, "Ready upload artifact is missing."
            )
        media = session.scalar(
            select(MediaAssetRow).where(MediaAssetRow.source_artifact_id == artifact.id)
        )
        package = session.scalar(
            select(PublishPackageRow).where(PublishPackageRow.upload_artifact_id == artifact.id)
        )
        return (
            upload_row_to_contract(upload),
            _artifact_ref(artifact),
            _media_asset_contract(media) if media else None,
            _publish_package_contract(package) if package else None,
        )

    def create_artifact_from_upload(
        self, upload: UploadSession, *, media_info: MediaInfo | None = None
    ) -> Artifact:
        existing = self.artifact_for_upload(upload.id)
        if existing is not None:
            return existing
        return self.create_artifact(
            kind=ArtifactKind.uploaded_file,
            uri=upload.object_uri,
            size_bytes=upload.size_bytes,
            sha256=upload.sha256,
            media_info=media_info,
            payload_schema="UploadedFileArtifact.v1",
            payload=upload.model_dump(mode="json"),
            source_upload_session_id=upload.id,
        )

    def artifact_for_upload(self, upload_session_id: str) -> Artifact | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ArtifactRow).where(ArtifactRow.source_upload_session_id == upload_session_id)
            )
            return artifact_row_to_contract(row) if row else None

    def create_artifact(
        self,
        *,
        artifact_id: str | None = None,
        kind: ArtifactKind,
        payload_schema: str,
        payload,
        uri: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        media_info: MediaInfo | None = None,
        source_upload_session_id: str | None = None,
    ) -> Artifact:
        with self.session_factory() as session:
            row = ArtifactRow(
                id=artifact_id or new_id("art"),
                kind=kind.value,
                uri=uri,
                size_bytes=size_bytes,
                sha256=sha256,
                media_info=media_info.model_dump(mode="json") if media_info else None,
                payload_schema=payload_schema,
                payload=payload,
                source_upload_session_id=source_upload_session_id,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.get(ArtifactRow, artifact_id) if artifact_id else None
                if existing is None and source_upload_session_id is not None:
                    existing = session.scalar(
                        select(ArtifactRow).where(
                            ArtifactRow.source_upload_session_id == source_upload_session_id
                        )
                    )
                if existing is None:
                    raise
                return artifact_row_to_contract(existing)
            session.refresh(row)
            return artifact_row_to_contract(row)

    def artifact_ref(self, artifact_id: str) -> ArtifactRef:
        with self.session_factory() as session:
            row = session.get(ArtifactRow, artifact_id)
            if row is None:
                raise KeyError(artifact_id)
            return _artifact_ref(row)

    def set_upload_thumbnail(self, upload_session_id: str, thumbnail_uri: str) -> None:
        with self.session_factory() as session:
            artifact = session.scalar(
                select(ArtifactRow).where(ArtifactRow.source_upload_session_id == upload_session_id)
            )
            if artifact is None:
                return
            media = session.scalar(
                select(MediaAssetRow).where(MediaAssetRow.source_artifact_id == artifact.id)
            )
            if media is None or media.thumbnail_uri == thumbnail_uri:
                return
            media.thumbnail_uri = thumbnail_uri
            media.updated_at = utcnow()
            session.commit()
