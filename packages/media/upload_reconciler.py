from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import logging
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from uuid import uuid4

from packages.core import contracts as c
from packages.core.config.settings import UploadSettings
from packages.core.contracts.media import UPLOAD_KIND_MAX_SIZE_BYTES
from packages.core.storage.object_store import (
    MultipartPart,
    ObjectStore,
    parse_object_uri,
    sha256_file,
)
from packages.core.storage.sqlalchemy_uploads import SqlAlchemyUploadRepository
from packages.core.workflow import NodeExecutionError
from packages.media.assets import local_object_path
from packages.media.cover_image import (
    THUMBNAIL_SUFFIX,
    WEB_THUMBNAIL_LABEL,
    build_cover_thumbnail_file,
)
from packages.media.video.ffmpeg import (
    FfmpegCommandError,
    extract_thumbnails,
    normalize_for_upload,
    probe_media,
    stabilize_video,
)

logger = logging.getLogger("cutagent.uploads")
_CLEANUP_TERMINAL_STATUSES = {
    c.UploadStatus.rejected,
    c.UploadStatus.failed,
    c.UploadStatus.cancelled,
    c.UploadStatus.expired,
}


class UploadRejectedError(ValueError):
    """The uploaded bytes are permanently invalid and must not be retried."""

    def __init__(
        self,
        message: str,
        code: c.ErrorCode = c.ErrorCode.upload_unsupported_type,
    ) -> None:
        super().__init__(message)
        self.code = code


class UploadReconciler:
    """Crash-safe state-machine driver for completed browser uploads.

    Every durable step is committed before the next external side effect. Re-running
    ``process`` after a crash therefore either observes the previous side effect
    (HEAD/ListParts) or repeats an operation against the same deterministic key.
    """

    def __init__(
        self,
        repository: SqlAlchemyUploadRepository,
        object_store: ObjectStore,
        settings: UploadSettings,
        *,
        owner: str | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.settings = settings
        self.owner = owner or f"upload-reconciler-{uuid4().hex}"

    def reconcile_once(self, *, limit: int = 10) -> int:
        expired = self.repository.expire_stale_uploads(limit=limit)
        for upload in expired:
            self._cleanup_upload(upload)
        claimed = self.repository.claim_reconcilable(
            owner=self.owner,
            limit=limit,
            lease_seconds=self.settings.reconcile_lease_seconds,
        )
        for upload in claimed:
            self.process(upload.id, _lease_owner=self.owner)
        derivations = self.repository.claim_ready_derivations(
            owner=self.owner,
            limit=max(0, limit - len(claimed)),
            lease_seconds=self.settings.reconcile_lease_seconds,
        )
        for upload in derivations:
            self.process(upload.id, _lease_owner=self.owner)
        return len(expired) + len(claimed) + len(derivations)

    def process(
        self,
        upload_session_id: str,
        *,
        raise_on_rejected: bool = False,
        _lease_owner: str | None = None,
    ) -> c.UploadSession:
        # Batch worker claims reuse ``self.owner``; direct API/background calls
        # receive a unique token so two requests in one replica cannot share a
        # lease merely because they share one reconciler instance.
        lease_owner = _lease_owner or f"{self.owner}:run:{uuid4().hex}"
        claimed = self.repository.claim_for_processing(
            upload_session_id,
            owner=lease_owner,
            lease_seconds=self.settings.reconcile_lease_seconds,
        )
        if claimed is None:
            current = self.repository.get_upload(upload_session_id)
            if current is None:
                raise KeyError(upload_session_id)
            if current.status in _CLEANUP_TERMINAL_STATUSES:
                self._cleanup_upload(current)
            return current
        with self._lease_heartbeat(upload_session_id, lease_owner):
            return self._process_claimed(
                upload_session_id,
                lease_owner=lease_owner,
                raise_on_rejected=raise_on_rejected,
            )

    def _process_claimed(
        self,
        upload_session_id: str,
        *,
        lease_owner: str,
        raise_on_rejected: bool,
    ) -> c.UploadSession:
        try:
            # A normal request can traverse all three durable stages immediately;
            # a crash at any boundary leaves the current status for the next pass.
            for _ in range(4):
                upload = self.repository.get_upload(upload_session_id)
                if upload is None:
                    raise KeyError(upload_session_id)
                if upload.status == c.UploadStatus.completing:
                    self._complete_object(upload, lease_owner=lease_owner)
                    continue
                if upload.status == c.UploadStatus.object_completed:
                    self._verify_and_promote(upload, lease_owner=lease_owner)
                    continue
                if upload.status == c.UploadStatus.verified:
                    self._assert_verified_object_exists(upload)
                    upload, _, _, _ = self.repository.finalize_ready(
                        upload.id,
                        release_lease=False,
                        lease_owner=lease_owner,
                    )
                    self._cleanup_staging_after_promotion(upload)
                    self._derive_ready_artifacts(upload, lease_owner=lease_owner)
                    return upload
                if upload.status == c.UploadStatus.ready:
                    self._cleanup_staging_after_promotion(upload)
                    self._derive_ready_artifacts(upload, lease_owner=lease_owner)
                    return upload
                if upload.status in _CLEANUP_TERMINAL_STATUSES:
                    self._cleanup_upload(upload)
                self.repository.clear_lease(upload.id, lease_owner=lease_owner)
                return upload
            raise RuntimeError("Upload reconciler exceeded the state transition budget.")
        except UploadRejectedError as exc:
            upload = self.repository.get_upload(upload_session_id)
            if upload is not None:
                rejected = self.repository.reject_upload(
                    upload.id,
                    str(exc),
                    lease_owner=lease_owner,
                )
                if rejected.status == c.UploadStatus.rejected:
                    self._cleanup_upload(rejected)
                if raise_on_rejected and rejected.status == c.UploadStatus.rejected:
                    raise NodeExecutionError(exc.code, str(exc))
                return rejected
            raise
        except Exception as exc:  # noqa: BLE001 - persisted retry taxonomy boundary
            current = self.repository.get_upload(upload_session_id)
            if current is not None and current.status in _CLEANUP_TERMINAL_STATUSES:
                # cancel/expiry may win while this worker is copying a deterministic
                # final object. Remove both known and derivable keys after observing
                # the terminal winner instead of retrying an illegal transition.
                self._cleanup_upload(current)
                self.repository.clear_lease(
                    upload_session_id,
                    lease_owner=lease_owner,
                )
                return current
            if current is not None and current.status == c.UploadStatus.ready:
                upload = self.repository.record_derivation_retry(
                    upload_session_id,
                    f"{type(exc).__name__}: {exc}",
                    lease_owner=lease_owner,
                )
                logger.warning(
                    "upload derivative generation failed after ready",
                    extra={
                        "event": "upload_derivation_failed",
                        "upload_session_id": upload_session_id,
                        "retry_count": upload.retry_count,
                    },
                    exc_info=True,
                )
                return upload
            upload = self.repository.record_retry(
                upload_session_id,
                f"{type(exc).__name__}: {exc}",
                max_retries=self.settings.reconcile_max_retries,
                lease_owner=lease_owner,
            )
            logger.warning(
                "upload reconciliation failed",
                extra={
                    "event": "upload_reconcile_failed",
                    "upload_session_id": upload_session_id,
                    "retry_count": upload.retry_count,
                    "status": upload.status.value,
                },
                exc_info=True,
            )
            if upload.status == c.UploadStatus.failed:
                self._cleanup_upload(upload)
            return upload

    @contextmanager
    def _lease_heartbeat(self, upload_session_id: str, lease_owner: str) -> Iterator[None]:
        stop = Event()
        interval = max(
            0.25,
            min(self.settings.reconcile_lease_seconds / 3, 30.0),
        )

        def renew() -> None:
            while not stop.wait(interval):
                try:
                    renewed = self.repository.renew_processing_lease(
                        upload_session_id,
                        owner=lease_owner,
                        lease_seconds=self.settings.reconcile_lease_seconds,
                    )
                except Exception:  # noqa: BLE001 - transient DB errors may recover next tick
                    logger.warning(
                        "upload lease heartbeat failed",
                        extra={
                            "event": "upload_lease_heartbeat_failed",
                            "upload_session_id": upload_session_id,
                        },
                        exc_info=True,
                    )
                    continue
                if not renewed:
                    logger.warning(
                        "upload processing lease was lost",
                        extra={
                            "event": "upload_lease_lost",
                            "upload_session_id": upload_session_id,
                        },
                    )
                    return

        thread = Thread(
            target=renew,
            name=f"upload-lease-{upload_session_id}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)

    def cleanup_terminal(self, upload: c.UploadSession) -> None:
        """Idempotently remove remote state after a persisted terminal transition."""

        self._cleanup_upload(upload)

    def _derive_ready_artifacts(
        self, upload: c.UploadSession, *, lease_owner: str | None = None
    ) -> None:
        source = self.repository.artifact_for_upload(upload.id)
        if source is None:
            raise RuntimeError("Ready upload source artifact is missing.")
        if source.uri is None or source.media_info is None:
            self.repository.mark_derivation_complete(upload.id, lease_owner=lease_owner)
            return
        media_type = source.media_info.media_type
        if media_type not in {"video", "image"}:
            self.repository.mark_derivation_complete(upload.id, lease_owner=lease_owner)
            return

        source_path = local_object_path(self.object_store, source.uri)
        thumbnail_source = source_path
        if media_type == "video":
            with TemporaryDirectory(prefix=f"cutagent-upload-{upload.id}-") as directory:
                frames = extract_thumbnails(source_path, Path(directory))
                for frame in frames:
                    ref = self.object_store.prepare_upload(
                        frame.path.name,
                        "thumbnails",
                        content_key=f"{source.id}-{frame.label}",
                    )
                    stored = self.object_store.upload_file(
                        frame.path, ref, content_type="image/png"
                    )
                    self.repository.create_artifact(
                        artifact_id=_derived_artifact_id(
                            source.id, c.ArtifactKind.cover_image, frame.label
                        ),
                        kind=c.ArtifactKind.cover_image,
                        payload_schema="uri-only",
                        payload={
                            "source_artifact_id": source.id,
                            "thumbnail_label": frame.label,
                        },
                        uri=stored.ref.uri,
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        media_info=frame.media_info,
                    )
                thumbnail_source = frames[-1].path
                web_uri = self._create_web_thumbnail(source.id, thumbnail_source)
        else:
            web_uri = self._create_web_thumbnail(source.id, thumbnail_source)
        self.repository.set_upload_thumbnail(upload.id, web_uri)
        self.repository.mark_derivation_complete(upload.id, lease_owner=lease_owner)

    def _create_web_thumbnail(self, source_artifact_id: str, source: Path) -> str:
        content = build_cover_thumbnail_file(source)
        ref = self.object_store.prepare_upload(
            f"{WEB_THUMBNAIL_LABEL}{THUMBNAIL_SUFFIX}",
            "thumbnails",
            content_key=f"{source_artifact_id}-{WEB_THUMBNAIL_LABEL}",
        )
        stored = self.object_store.put_bytes(ref, content)
        artifact = self.repository.create_artifact(
            artifact_id=_derived_artifact_id(
                source_artifact_id,
                c.ArtifactKind.cover_thumbnail,
                WEB_THUMBNAIL_LABEL,
            ),
            kind=c.ArtifactKind.cover_thumbnail,
            payload_schema="uri-only",
            payload={
                "source_artifact_id": source_artifact_id,
                "thumbnail_label": WEB_THUMBNAIL_LABEL,
            },
            uri=stored.ref.uri,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
        )
        if artifact.uri is None:
            raise RuntimeError("Derived thumbnail artifact has no URI.")
        return artifact.uri

    def _complete_object(
        self, upload: c.UploadSession, *, lease_owner: str | None = None
    ) -> None:
        staging_uri = upload.staging_uri or upload.object_uri
        if staging_uri is None:
            raise UploadRejectedError(
                "Upload session has no staging object URI.",
                c.ErrorCode.upload_invalid_state,
            )
        ref = parse_object_uri(staging_uri)
        if not self.object_store.exists(ref):
            if upload.upload_strategy != c.UploadStrategy.multipart:
                raise UploadRejectedError(
                    "Uploaded object was not found in storage.",
                    c.ErrorCode.artifact_missing,
                )
            multipart_upload_id = self.repository.multipart_upload_id(upload.id)
            if not multipart_upload_id:
                raise UploadRejectedError(
                    "Multipart upload id is missing.",
                    c.ErrorCode.upload_invalid_state,
                )
            parts = self.object_store.list_parts(staging_uri, upload_id=multipart_upload_id)
            self._validate_completed_parts(upload, parts)
            # The ordered list comes from ListParts, never from the browser.
            self.object_store.complete_multipart_upload(
                staging_uri,
                upload_id=multipart_upload_id,
                parts=parts,
            )
        self.repository.patch_upload(
            upload.id,
            {
                "status": c.UploadStatus.object_completed,
                "last_error": None,
                "retry_count": 0,
                "next_retry_at": None,
            },
            lease_owner=lease_owner,
        )

    @staticmethod
    def _validate_completed_parts(upload: c.UploadSession, parts: list[MultipartPart]) -> None:
        if upload.part_size_bytes is None or upload.part_count < 1:
            raise UploadRejectedError(
                "Multipart geometry is missing.", c.ErrorCode.upload_invalid_state
            )
        by_number = {part.part_number: part for part in parts}
        expected_numbers = set(range(1, upload.part_count + 1))
        if set(by_number) != expected_numbers:
            missing = sorted(expected_numbers - set(by_number))
            raise UploadRejectedError(
                f"Multipart upload is missing parts: {missing}.",
                c.ErrorCode.upload_invalid_state,
            )
        for number in range(1, upload.part_count + 1):
            remaining = upload.size_bytes - (number - 1) * upload.part_size_bytes
            expected_size = min(upload.part_size_bytes, remaining)
            if by_number[number].size_bytes != expected_size:
                raise UploadRejectedError(
                    f"Multipart part {number} size mismatch: "
                    f"expected {expected_size}, got {by_number[number].size_bytes}.",
                    c.ErrorCode.upload_size_mismatch,
                )

    def _verify_and_promote(
        self, upload: c.UploadSession, *, lease_owner: str | None = None
    ) -> None:
        staging_uri = upload.staging_uri or upload.object_uri
        if staging_uri is None:
            raise UploadRejectedError(
                "Upload session has no staging object URI.",
                c.ErrorCode.upload_invalid_state,
            )
        try:
            head = self.object_store.head(staging_uri)
        except FileNotFoundError as exc:
            raise UploadRejectedError(
                "Uploaded object disappeared before verification.",
                c.ErrorCode.artifact_missing,
            ) from exc
        if head.size != upload.size_bytes:
            raise UploadRejectedError(
                f"Upload size mismatch: expected {upload.size_bytes}, got {head.size}.",
                c.ErrorCode.upload_size_mismatch,
            )
        if head.size > self.settings.max_size_bytes:
            raise UploadRejectedError(
                "Upload exceeds the 200 MiB product limit.",
                c.ErrorCode.upload_too_large,
            )
        kind_limit = UPLOAD_KIND_MAX_SIZE_BYTES.get(upload.kind)
        if kind_limit is not None and head.size > kind_limit:
            raise UploadRejectedError(
                f"{upload.kind.value} upload exceeds its product limit.",
                c.ErrorCode.upload_too_large,
            )
        if head.content_type and head.content_type != upload.content_type:
            raise UploadRejectedError(
                f"Upload content-type mismatch: expected {upload.content_type}, "
                f"got {head.content_type}.",
                c.ErrorCode.upload_unsupported_type,
            )

        source_path = local_object_path(self.object_store, staging_uri)
        canonical_sha256 = sha256_file(source_path)
        expected_sha256 = upload.client_expected_sha256
        if expected_sha256 and canonical_sha256 != expected_sha256:
            raise UploadRejectedError("Upload sha256 mismatch.", c.ErrorCode.upload_sha256_mismatch)
        media_info = self._validate_file(upload, source_path)

        processed_path = source_path
        stabilized = upload.stabilized
        was_normalized = upload.normalized
        metadata = dict(upload.completion_metadata)
        # Never write normalization/stabilization intermediates beside an object
        # store cache path. A rejected upload or process crash must not leak local
        # derivatives that are invisible to terminal object cleanup.
        with TemporaryDirectory(prefix=f"cutagent-upload-process-{upload.id}-") as directory:
            work_dir = Path(directory)
            if upload.kind == c.UploadKind.video and self.settings.normalize_video:
                try:
                    normalization = normalize_for_upload(
                        processed_path,
                        work_dir / "normalized.mp4",
                    )
                except FfmpegCommandError as exc:
                    raise UploadRejectedError("上传视频规范化失败，文件无法安全解析。") from exc
                processed_path = normalization.output_path
                media_info = normalization.media_info
                metadata["normalized"] = "1"
                was_normalized = True
            if upload.kind == c.UploadKind.video and upload.stabilize:
                try:
                    processed_path = stabilize_video(
                        processed_path,
                        work_dir / "stabilized.mp4",
                    )
                    media_info = probe_media(processed_path)
                except FfmpegCommandError as exc:
                    raise UploadRejectedError("上传视频增稳失败，文件无法安全解析。") from exc
                stabilized = True

            final_uri = self._final_uri_for(staging_uri, upload.kind)
            if processed_path == source_path:
                self.object_store.copy(staging_uri, final_uri)
                final_sha256 = canonical_sha256
            else:
                final_ref = parse_object_uri(final_uri)
                stored = self.object_store.upload_file(
                    processed_path,
                    final_ref,
                    content_type=upload.content_type,
                )
                final_sha256 = stored.sha256
            final_head = self.object_store.head(final_uri)

        # Persist verified before deleting staging. If the process exits after the
        # commit, the next pass registers resources and cleanup is safely repeatable.
        self.repository.patch_upload(
            upload.id,
            {
                "status": c.UploadStatus.verified,
                "canonical_sha256": canonical_sha256,
                "sha256": final_sha256,
                "final_size_bytes": final_head.size,
                "final_uri": final_uri,
                "object_uri": final_uri,
                "verified_media_info": media_info,
                "stabilized": stabilized,
                "normalized": was_normalized,
                "completion_metadata": metadata,
                "last_error": None,
                "retry_count": 0,
                "next_retry_at": None,
            },
            lease_owner=lease_owner,
        )
        self._safe_delete(staging_uri)

    def _validate_file(self, upload: c.UploadSession, path: Path) -> c.MediaInfo | None:
        prefix = path.read_bytes()[:16] if path.stat().st_size <= 16 else _read_prefix(path, 16)
        if not _header_matches_content_type(prefix, upload.content_type):
            raise UploadRejectedError("文件头与声明的 Content-Type 不一致。")
        if upload.kind == c.UploadKind.font:
            try:
                from fontTools.ttLib import TTFont

                font = TTFont(str(path), fontNumber=0, lazy=True)
                font.close()
            except Exception as exc:  # noqa: BLE001 - parser rejects corrupt fonts
                raise UploadRejectedError("字体文件无法解析或已损坏。") from exc
            return None

        expected_media_type = {
            c.UploadKind.video: "video",
            c.UploadKind.publish_video: "video",
            c.UploadKind.image: "image",
            c.UploadKind.cover_template: "image",
            c.UploadKind.voice_reference: "audio",
            c.UploadKind.bgm: "audio",
        }.get(upload.kind)
        if expected_media_type is None:
            return None
        try:
            media_info = probe_media(path)
        except FfmpegCommandError as exc:
            raise UploadRejectedError("上传的媒体文件无法解析或已损坏。") from exc
        if media_info.media_type != expected_media_type:
            raise UploadRejectedError(
                f"文件实际类型为 {media_info.media_type}，预期 {expected_media_type}。"
            )
        return media_info

    def _final_uri_for(self, staging_uri: str, kind: c.UploadKind) -> str:
        segments = parse_object_uri(staging_uri).key.split("/")
        content_key, filename = segments[-2], segments[-1]
        return self.object_store.prepare_upload(filename, kind.value, content_key=content_key).uri

    def _cleanup_upload(self, upload: c.UploadSession) -> None:
        multipart_upload_id = self.repository.multipart_upload_id(upload.id)
        staging_uri = upload.staging_uri or upload.object_uri
        if (
            multipart_upload_id
            and staging_uri
            and upload.upload_strategy == c.UploadStrategy.multipart
        ):
            try:
                self.object_store.abort_multipart_upload(staging_uri, upload_id=multipart_upload_id)
            except Exception:  # noqa: BLE001 - best-effort terminal cleanup
                pass
        cleanup_uris = {staging_uri, upload.final_uri}
        if staging_uri:
            try:
                cleanup_uris.add(self._final_uri_for(staging_uri, upload.kind))
            except (ValueError, IndexError):
                # Legacy/malformed rows can still be cleaned by their persisted keys.
                pass
        for uri in cleanup_uris:
            if uri:
                self._safe_delete(uri)

    def _cleanup_staging_after_promotion(self, upload: c.UploadSession) -> None:
        staging_uri = upload.staging_uri
        if staging_uri and staging_uri != upload.final_uri:
            self._safe_delete(staging_uri)

    def _assert_verified_object_exists(self, upload: c.UploadSession) -> None:
        final_uri = upload.final_uri or upload.object_uri
        if final_uri is None:
            raise UploadRejectedError(
                "Verified upload has no final object URI.",
                c.ErrorCode.artifact_missing,
            )
        try:
            head = self.object_store.head(final_uri)
        except FileNotFoundError as exc:
            raise UploadRejectedError(
                "Verified upload final object is missing.",
                c.ErrorCode.artifact_missing,
            ) from exc
        expected_size = upload.final_size_bytes or upload.size_bytes
        if head.size != expected_size:
            raise UploadRejectedError(
                f"Verified object size mismatch: expected {expected_size}, got {head.size}.",
                c.ErrorCode.upload_size_mismatch,
            )

    def _safe_delete(self, uri: str) -> None:
        try:
            self.object_store.delete(uri)
        except Exception:  # noqa: BLE001 - retry-safe best effort cleanup
            pass


def _read_prefix(path: Path, length: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(length)


def _derived_artifact_id(source_artifact_id: str, kind: c.ArtifactKind, label: str) -> str:
    digest = sha256(f"{source_artifact_id}:{kind.value}:{label}".encode()).hexdigest()
    return f"art_derived_{digest[:24]}"


def _header_matches_content_type(prefix: bytes, content_type: str) -> bool:
    if content_type in {"video/mp4", "video/quicktime", "audio/mp4"}:
        return len(prefix) >= 8 and prefix[4:8] == b"ftyp"
    if content_type == "video/webm":
        return prefix.startswith(b"\x1aE\xdf\xa3")
    if content_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    if content_type in {"audio/wav", "audio/x-wav"}:
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE"
    if content_type == "audio/mpeg":
        return prefix.startswith(b"ID3") or (
            len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
        )
    if content_type == "audio/aac":
        return len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xF6 == 0xF0
    if content_type.startswith("font/") or content_type in {
        "application/x-font-ttf",
        "application/vnd.ms-opentype",
    }:
        return prefix.startswith((b"\x00\x01\x00\x00", b"OTTO", b"wOFF", b"wOF2"))
    return False
