from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from math import ceil

from fastapi import BackgroundTasks, Request

from apps.api.common import object_store, request_id, settings, upload_repository
from packages.core import contracts as c
from packages.core.contracts.media import (
    ALLOWED_UPLOAD_CONTENT_TYPES,
    UPLOAD_KIND_MAX_SIZE_BYTES,
)
from packages.core.storage.object_store import ObjectStore
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.media import UploadReconciler

_STAGING_PURPOSE = "incoming/uploads"
_SIGNABLE_UPLOAD_STATUSES = {
    c.UploadStatus.prepared,
    c.UploadStatus.uploading,
}


def _visual_asset_kind_and_tags(upload_kind: c.UploadKind) -> tuple[str, list[str]]:
    """Keep the shared visual-kind normalization seam used by ingestion tests."""

    persisted_kind, legacy_tag = c.normalize_visual_asset_kind(upload_kind.value)
    tags = [persisted_kind, "upload"]
    if legacy_tag is not None:
        tags.append(legacy_tag)
    return persisted_kind, tags


def prepare_upload(
    payload: c.PrepareUploadRequest,
    request: Request,
    user: c.AuthUser,
) -> c.PrepareUploadResponse:
    store = object_store(request)
    repository = upload_repository(request)
    upload_settings = settings(request).upload
    if not store.supports_presign():
        raise NodeExecutionError(
            c.ErrorCode.upload_invalid_state,
            "Object store backend does not support presigned uploads.",
        )
    if payload.size_bytes > upload_settings.max_size_bytes:
        raise NodeExecutionError(c.ErrorCode.upload_too_large, "Upload exceeds 200 MiB.")
    kind_limit = UPLOAD_KIND_MAX_SIZE_BYTES.get(payload.kind)
    if kind_limit is not None and payload.size_bytes > kind_limit:
        raise NodeExecutionError(
            c.ErrorCode.upload_too_large,
            f"{payload.kind.value} uploads are limited to {kind_limit // 1024 // 1024} MiB.",
        )
    if payload.content_type not in ALLOWED_UPLOAD_CONTENT_TYPES.get(payload.kind, frozenset()):
        raise NodeExecutionError(
            c.ErrorCode.upload_unsupported_type,
            f"Content type {payload.content_type!r} is not allowed for {payload.kind.value}.",
        )

    existing = repository.get_upload_by_client_id(payload.client_upload_id)
    if existing is not None:
        _assert_access(existing, user)
        _assert_prepare_matches(existing, payload)
        upload = _refresh_expiration(request, existing)
        _raise_if_expired(upload)
    else:
        strategy = (
            c.UploadStrategy.multipart
            if payload.size_bytes >= upload_settings.multipart_threshold_bytes
            else c.UploadStrategy.single
        )
        part_size = (
            upload_settings.part_size_bytes if strategy == c.UploadStrategy.multipart else None
        )
        part_count = ceil(payload.size_bytes / part_size) if part_size else 1
        upload_id = new_id("upl")
        staging_ref = store.prepare_upload(
            payload.filename, _STAGING_PURPOSE, content_key=upload_id
        )
        upload = repository.create_upload(
            c.UploadSession(
                id=upload_id,
                client_upload_id=payload.client_upload_id,
                owner_user_id=user.id,
                kind=payload.kind,
                case_id=payload.case_id,
                filename=payload.filename,
                content_type=payload.content_type,
                size_bytes=payload.size_bytes,
                sha256=payload.sha256,
                client_expected_sha256=payload.sha256,
                upload_strategy=strategy,
                part_size_bytes=part_size,
                part_count=part_count,
                object_uri=staging_ref.uri,
                staging_uri=staging_ref.uri,
                stabilize=payload.stabilize,
                expires_at=c.utcnow() + timedelta(days=1),
            )
        )

    staging_uri = upload.staging_uri or upload.object_uri
    if staging_uri is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload staging URI is missing.")
    signed = None
    if upload.status in _SIGNABLE_UPLOAD_STATUSES:
        ttl = _presign_ttl(upload, upload_settings.presign_ttl_seconds)
        if upload.upload_strategy == c.UploadStrategy.multipart:
            repository.ensure_multipart_upload_id(
                upload.id,
                lambda: store.create_multipart_upload(
                    staging_uri,
                    content_type=upload.content_type,
                ),
            )
        else:
            signed = store.signed_put_url(
                staging_uri,
                content_type=upload.content_type,
                expires_in=ttl,
            )
    return c.PrepareUploadResponse(
        upload_session=upload,
        upload_strategy=upload.upload_strategy,
        part_size_bytes=upload.part_size_bytes,
        part_count=upload.part_count,
        put_url=signed.url if signed else None,
        put_content_type=upload.content_type,
        expires_at=signed.expires_at if signed else None,
    )


def sign_upload_parts(
    upload_session_id: str,
    payload: c.SignUploadPartsRequest,
    request: Request,
    user: c.AuthUser,
) -> c.SignUploadPartsResponse:
    upload = _load_upload(request, upload_session_id, user)
    if upload.upload_strategy != c.UploadStrategy.multipart:
        raise NodeExecutionError(
            c.ErrorCode.upload_invalid_state, "Upload session does not use multipart."
        )
    if upload.status not in _SIGNABLE_UPLOAD_STATUSES:
        raise NodeExecutionError(
            c.ErrorCode.upload_invalid_state,
            f"Parts cannot be signed from status {upload.status.value}.",
        )
    invalid = [part for part in payload.part_numbers if part > upload.part_count]
    if invalid:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            f"Part numbers exceed part_count={upload.part_count}: {invalid}.",
        )
    staging_uri = upload.staging_uri or upload.object_uri
    multipart_upload_id = upload_repository(request).multipart_upload_id(upload.id)
    if not staging_uri or not multipart_upload_id:
        raise NodeExecutionError(
            c.ErrorCode.upload_invalid_state, "Multipart upload metadata is missing."
        )
    completed = {
        part.part_number
        for part in object_store(request).list_parts(staging_uri, upload_id=multipart_upload_id)
    }
    ttl = _presign_ttl(upload, settings(request).upload.presign_ttl_seconds)
    signed_parts = []
    for part_number in payload.part_numbers:
        if part_number in completed:
            continue
        signed = object_store(request).sign_upload_part(
            staging_uri,
            upload_id=multipart_upload_id,
            part_number=part_number,
            expires_in=ttl,
        )
        signed_parts.append(
            c.SignedUploadPart(
                part_number=part_number,
                put_url=signed.url,
                expires_at=signed.expires_at,
            )
        )
    if upload.status == c.UploadStatus.prepared:
        upload = upload_repository(request).patch_upload(
            upload.id, {"status": c.UploadStatus.uploading}
        )
    return c.SignUploadPartsResponse(upload_session=upload, parts=signed_parts)


def resume_upload(
    upload_session_id: str,
    request: Request,
    user: c.AuthUser,
) -> c.ResumeUploadResponse:
    upload = _load_upload(request, upload_session_id, user)
    parts: list[c.UploadPart] = []
    if upload.upload_strategy == c.UploadStrategy.multipart and upload.status in {
        c.UploadStatus.prepared,
        c.UploadStatus.uploading,
    }:
        staging_uri = upload.staging_uri or upload.object_uri
        multipart_upload_id = upload_repository(request).multipart_upload_id(upload.id)
        if staging_uri and multipart_upload_id:
            parts = [
                c.UploadPart(
                    part_number=part.part_number,
                    etag=part.etag,
                    size_bytes=part.size_bytes,
                )
                for part in object_store(request).list_parts(
                    staging_uri, upload_id=multipart_upload_id
                )
            ]
    artifact = None
    media_asset = None
    publish_package = None
    if upload.status == c.UploadStatus.ready:
        upload, artifact, media_asset, publish_package = upload_repository(request).ready_resources(
            upload.id
        )
    return c.ResumeUploadResponse(
        upload_session=upload,
        completed_parts=parts,
        artifact=artifact,
        media_asset=media_asset,
        publish_package=publish_package,
        request_id=request_id(),
    )


def object_complete_upload(
    upload_session_id: str,
    payload: c.ObjectCompleteUploadRequest,
    request: Request,
    user: c.AuthUser,
    background_tasks: BackgroundTasks,
) -> c.ObjectCompleteUploadResponse:
    upload = _load_upload(request, upload_session_id, user)
    upload = upload_repository(request).mark_completing(
        upload.id,
        size_bytes=payload.size_bytes,
        expected_sha256=payload.sha256,
        metadata=payload.metadata,
    )
    if upload.status == c.UploadStatus.expired:
        _reconciler(request).cleanup_terminal(upload)
        _raise_if_expired(upload)
    if upload.status == c.UploadStatus.ready:
        upload, artifact, media_asset, publish_package = upload_repository(request).ready_resources(
            upload.id
        )
        return c.ObjectCompleteUploadResponse(
            upload_session=upload,
            artifact=artifact,
            media_asset=media_asset,
            publish_package=publish_package,
            request_id=request_id(),
        )
    background_tasks.add_task(_reconciler(request).process, upload.id)
    return c.ObjectCompleteUploadResponse(
        upload_session=upload,
        request_id=request_id(),
    )


def complete_upload(
    payload: c.CompleteUploadRequest,
    request: Request,
    user: c.AuthUser,
) -> c.CompleteUploadResponse:
    """Compatibility adapter over the #210 durable state machine.

    Old callers still receive a ready resource response synchronously, but all
    object completion, verification and registration use the same reconciler as
    the new 202 endpoint.
    """

    upload = _load_upload(request, payload.upload_session_id, user)
    expected_sha256 = payload.sha256 or upload.client_expected_sha256 or upload.sha256
    upload = upload_repository(request).mark_completing(
        upload.id,
        size_bytes=payload.size_bytes or upload.size_bytes,
        expected_sha256=expected_sha256,
        metadata=payload.metadata,
    )
    if upload.status == c.UploadStatus.expired:
        _reconciler(request).cleanup_terminal(upload)
        _raise_if_expired(upload)
    upload = _reconciler(request).process(upload.id, raise_on_rejected=True)
    if upload.status != c.UploadStatus.ready:
        code = (
            c.ErrorCode.upload_unsupported_type
            if upload.status == c.UploadStatus.rejected
            else c.ErrorCode.upload_invalid_state
        )
        raise NodeExecutionError(code, upload.last_error or "Upload processing failed.")
    upload, artifact, media_asset, publish_package = upload_repository(request).ready_resources(
        upload.id
    )
    return c.CompleteUploadResponse(
        upload_session=upload,
        artifact=artifact,
        media_asset=media_asset,
        publish_package=publish_package,
        request_id=request_id(),
    )


def cancel_upload(
    upload_session_id: str,
    request: Request,
    user: c.AuthUser,
) -> c.UploadSession:
    upload = _load_upload(request, upload_session_id, user)
    if upload.status == c.UploadStatus.cancelled:
        return upload
    if upload.status in {
        c.UploadStatus.ready,
        c.UploadStatus.rejected,
        c.UploadStatus.failed,
        c.UploadStatus.expired,
    }:
        raise NodeExecutionError(
            c.ErrorCode.upload_invalid_state,
            f"Upload cannot be cancelled from status {upload.status.value}.",
        )
    # Persist the terminal winner under a row lock before touching object storage.
    # An in-flight reconciler then observes cancelled and cleans any deterministic
    # final key it may have copied just before its stale transition was rejected.
    cancelled = upload_repository(request).patch_upload(
        upload_session_id, {"status": c.UploadStatus.cancelled}
    )
    _reconciler(request).cleanup_terminal(cancelled)
    return cancelled


def get_upload(
    upload_session_id: str,
    request: Request,
    user: c.AuthUser,
) -> c.UploadSession:
    return _load_upload(request, upload_session_id, user)


def _load_upload(request: Request, upload_id: str, user: c.AuthUser) -> c.UploadSession:
    upload = upload_repository(request).get_upload(upload_id)
    if upload is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
    _assert_access(upload, user)
    return _refresh_expiration(request, upload)


def _refresh_expiration(request: Request, upload: c.UploadSession) -> c.UploadSession:
    refreshed = upload_repository(request).expire_if_stale(upload.id)
    if refreshed.status == c.UploadStatus.expired:
        _reconciler(request).cleanup_terminal(refreshed)
    return refreshed


def _raise_if_expired(upload: c.UploadSession) -> None:
    if upload.status == c.UploadStatus.expired:
        raise NodeExecutionError(
            c.ErrorCode.upload_invalid_state,
            "Upload session expired; start a new upload.",
        )


def _presign_ttl(upload: c.UploadSession, configured_seconds: int) -> timedelta:
    configured = timedelta(seconds=configured_seconds)
    if upload.expires_at is None:
        return configured
    remaining = upload.expires_at - c.utcnow()
    # Reserve one second for signing/whole-second rounding so a URL minted near
    # the boundary still expires strictly before the durable session.
    if remaining <= timedelta(seconds=2):
        raise NodeExecutionError(
            c.ErrorCode.upload_invalid_state,
            "Upload session is too close to expiry; start a new upload.",
        )
    return min(configured, remaining - timedelta(seconds=1))


def _assert_access(upload: c.UploadSession, user: c.AuthUser) -> None:
    if user.role == c.UserRole.admin:
        return
    if upload.owner_user_id != user.id:
        # Do not disclose another operator's stable client_upload_id/session.
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Upload session not found.")


def _assert_prepare_matches(upload: c.UploadSession, payload: c.PrepareUploadRequest) -> None:
    if (
        upload.kind != payload.kind
        or upload.case_id != payload.case_id
        or upload.filename != payload.filename
        or upload.content_type != payload.content_type
        or upload.size_bytes != payload.size_bytes
        or upload.client_expected_sha256 != payload.sha256
        or upload.stabilize != payload.stabilize
    ):
        raise NodeExecutionError(
            c.ErrorCode.idempotency_conflict,
            "client_upload_id was already used for a different upload.",
        )


def _reconciler(request: Request) -> UploadReconciler:
    return request.app.state.upload_reconciler


def _safe_delete(store: ObjectStore, uri: str) -> None:
    try:
        store.delete(uri)
    except Exception:  # noqa: BLE001 - best-effort terminal cleanup
        pass


def _patch_upload(request: Request, upload_id: str, updates: dict) -> c.UploadSession:
    return upload_repository(request).patch_upload(upload_id, updates)


def _fail_upload(
    request: Request,
    store: ObjectStore,
    upload_id: str,
    staging_uri: str,
    derived_uris: Iterable[str] = (),
) -> None:
    """Legacy test/helper seam using the new terminal cleanup semantics."""

    _safe_delete(store, staging_uri)
    for uri in derived_uris:
        if uri and uri != staging_uri:
            _safe_delete(store, uri)
    try:
        _patch_upload(request, upload_id, {"status": c.UploadStatus.failed})
    except Exception:  # noqa: BLE001 - never mask the original failure
        pass
