from __future__ import annotations

from datetime import timedelta

from fastapi import Request

from apps.api.common import (
    media_repository,
    object_store,
    publishing_repository,
    request_id,
    settings,
    upload_repository,
)
from packages.core import contracts as c
from packages.core.contracts.media import ALLOWED_UPLOAD_CONTENT_TYPES
from packages.core.storage.object_store import ObjectStore, parse_object_uri, sha256_file
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.media.assets import local_object_path, store_file
from packages.media.video.ffmpeg import (
    FfmpegCommandError,
    extract_thumbnails,
    normalize_for_upload,
    probe_media,
    stabilize_video,
)

# Browser-writable staging prefix on the durable bucket. complete() server-side
# copies the verified object to its final, kind-routed key and drops staging.
_STAGING_PURPOSE = "incoming/uploads"


def prepare_upload(
    payload: c.PrepareUploadRequest, request: Request
) -> c.PrepareUploadResponse:
    store = object_store(request)
    if not store.supports_presign():
        raise NodeExecutionError(
            c.ErrorCode.upload_invalid_state,
            "Object store backend does not support presigned uploads.",
        )
    if payload.content_type not in ALLOWED_UPLOAD_CONTENT_TYPES.get(payload.kind, frozenset()):
        raise NodeExecutionError(
            c.ErrorCode.upload_unsupported_type,
            f"Content type {payload.content_type!r} is not allowed for {payload.kind.value}.",
        )
    upload_id = new_id("upl")
    # Stage to the durable bucket under a key keyed by the session id; complete()
    # re-derives the final key from this same id (see _final_uri_for).
    staging_ref = store.prepare_upload(payload.filename, _STAGING_PURPOSE, content_key=upload_id)
    ttl = timedelta(seconds=settings(request).upload.presign_ttl_seconds)
    signed = store.signed_put_url(
        staging_ref.uri, content_type=payload.content_type, expires_in=ttl
    )
    upload = c.UploadSession(
        id=upload_id,
        kind=payload.kind,
        case_id=payload.case_id,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        sha256=payload.sha256,
        object_uri=staging_ref.uri,
        stabilize=payload.stabilize,
    )
    upload = upload_repository(request).create_upload(upload)
    return c.PrepareUploadResponse(
        upload_session=upload,
        put_url=signed.url,
        put_content_type=payload.content_type,
        expires_at=signed.expires_at,
    )


def _load_upload(request: Request, upload_id: str) -> c.UploadSession:
    upload = upload_repository(request).get_upload(upload_id)
    if upload is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
    return upload


def _patch_upload(request: Request, upload_id: str, updates: dict) -> c.UploadSession:
    return upload_repository(request).patch_upload(upload_id, updates)


def _final_uri_for(store: ObjectStore, staging_uri: str, kind: c.UploadKind) -> str:
    # Staging key is "incoming/uploads/{upload_id}/{filename}"; reuse the same
    # upload_id (content_key) so the kind-routed final key is derived deterministically.
    segments = parse_object_uri(staging_uri).key.split("/")
    key_uuid, filename = segments[-2], segments[-1]
    return store.prepare_upload(filename, kind.value, content_key=key_uuid).uri


def _safe_delete(store: ObjectStore, uri: str) -> None:
    try:
        store.delete(uri)
    except Exception:  # noqa: BLE001 — best-effort staging cleanup, never block the caller
        pass


def _fail_upload(request: Request, store: ObjectStore, upload_id: str, staging_uri: str) -> None:
    _safe_delete(store, staging_uri)
    try:
        _patch_upload(request, upload_id, {"status": c.UploadStatus.failed})
    except Exception:  # noqa: BLE001 — never mask the original failure
        pass


def complete_upload(payload: c.CompleteUploadRequest, request: Request) -> c.CompleteUploadResponse:
    store = object_store(request)
    upload = _load_upload(request, payload.upload_session_id)
    staging_uri = upload.object_uri
    if staging_uri is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session has no object.")
    # First hop: the API never observed the browser's direct PUT.
    upload = _patch_upload(request, upload.id, {"status": c.UploadStatus.uploading})

    # Verify + post-process the uploaded object. ANY failure here drops the
    # staging object and fails the session — the API never held the bytes, so a
    # missing / oversize (size!=declared) / corrupt object only surfaces now.
    try:
        try:
            head = store.head(staging_uri)
        except NodeExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 — missing object / storage error
            raise NodeExecutionError(
                c.ErrorCode.upload_invalid_state, "Uploaded object not found in storage."
            ) from exc
        declared_size = payload.size_bytes if payload.size_bytes is not None else upload.size_bytes
        if declared_size is not None and head.size != declared_size:
            raise NodeExecutionError(c.ErrorCode.upload_size_mismatch, "Upload size mismatch.")
        if head.content_type and head.content_type != upload.content_type:
            raise NodeExecutionError(c.ErrorCode.upload_unsupported_type, "Upload content-type mismatch.")

        media_info = _probe_upload_media(request, upload)
        # The API never saw the bytes during upload, so recompute sha256 from the
        # downloaded object (the probe already pulled it into the local cache).
        actual_sha256 = sha256_file(local_object_path(store, staging_uri))
        if payload.sha256 and payload.sha256 != actual_sha256:
            raise NodeExecutionError(c.ErrorCode.upload_sha256_mismatch, "Upload sha256 mismatch.")
        upload = _patch_upload(request, upload.id, {"sha256": actual_sha256})

        was_normalized = False
        is_av_video = (
            media_info is not None
            and media_info.media_type == "video"
            and upload.kind in {c.UploadKind.portrait, c.UploadKind.broll, c.UploadKind.video}
        )
        if is_av_video and settings(request).upload.normalize_video:
            upload, media_info = _normalize_upload_video(request, upload)
            was_normalized = True
        if upload.stabilize and upload.kind in {c.UploadKind.portrait, c.UploadKind.broll, c.UploadKind.video}:
            upload, media_info = _stabilize_upload_video(request, upload)

        # Move the verified object from the browser-writable staging key to a
        # server-only final key (routed by kind), then drop staging. If
        # normalize/stabilize already rewrote object_uri to a server-written
        # object, only staging needs dropping.
        if upload.object_uri == staging_uri:
            final_uri = _final_uri_for(store, staging_uri, upload.kind)
            store.copy(staging_uri, final_uri)
            upload = _patch_upload(request, upload.id, {"object_uri": final_uri})
        if upload.object_uri != staging_uri:
            _safe_delete(store, staging_uri)
    except Exception:
        _fail_upload(request, store, upload.id, staging_uri)
        raise

    # Second hop: uploading -> completed, then register the artifact.
    upload = _patch_upload(request, upload.id, {"status": c.UploadStatus.completed})
    artifact = upload_repository(request).create_artifact_from_upload(upload, media_info=media_info)
    artifact_ref = upload_repository(request).artifact_ref(artifact.id)
    _create_upload_thumbnails(request, artifact)
    media_asset = None
    publish_package = None
    replace_mode = payload.metadata.get("template_mode") == "replace"
    if upload.kind in {
        c.UploadKind.portrait,
        c.UploadKind.broll,
        c.UploadKind.video,
        c.UploadKind.image,
        c.UploadKind.bgm,
        c.UploadKind.font,
        c.UploadKind.cover_template,
    } and not replace_mode:
        media_payload = c.CreateMediaAssetFromUploadRequest(
            upload_session_id=upload.id,
            case_id=upload.case_id,
            title=payload.metadata.get("title") or upload.filename,
            kind=upload.kind.value,
            tags=[upload.kind.value, "upload"],
        )
        if upload.stabilized:
            media_payload.tags.append("stabilized")
        if was_normalized:
            media_payload.tags.append("normalized")
        # 「AI素材」marker: the AI-source library tab uploads with this metadata flag
        # so the asset is tagged for the Seedance reference picker (no new contract
        # field needed — rides the existing metadata dict).
        if payload.metadata.get("ai_material") == "1" and "ai_material" not in media_payload.tags:
            media_payload.tags.append("ai_material")
        media_asset = media_repository(request).create_asset_from_upload(media_payload)
    elif upload.kind == c.UploadKind.publish_video:
        package_payload = c.CreatePublishPackageRequest(
            upload_artifact_id=artifact.id,
            title=payload.metadata.get("title") or upload.filename,
            description=payload.metadata.get("description", ""),
        )
        publish_package = publishing_repository(request).create_package(package_payload)
    return c.CompleteUploadResponse(
        upload_session=upload,
        artifact=artifact_ref,
        media_asset=media_asset,
        publish_package=publish_package,
        request_id=request_id(),
    )


def _probe_upload_media(request: Request, upload: c.UploadSession) -> c.MediaInfo | None:
    if not upload.object_uri or not upload.content_type.startswith(("video/", "audio/", "image/")):
        return None
    try:
        return probe_media(local_object_path(object_store(request), upload.object_uri))
    except FfmpegCommandError as exc:
        # 上传场景的探针失败是输入问题，错误码归 upload 域而非 render 域。
        raise NodeExecutionError(
            c.ErrorCode.upload_unsupported_type,
            "上传的媒体文件无法解析，请确认文件未损坏且为受支持的格式。",
        ) from exc


def _stabilize_upload_video(
    request: Request, upload: c.UploadSession
) -> tuple[c.UploadSession, c.MediaInfo]:
    if upload.object_uri is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Upload object is missing.")
    source_path = local_object_path(object_store(request), upload.object_uri)
    try:
        output_path = stabilize_video(source_path)
        media_info = probe_media(output_path)
    except FfmpegCommandError as exc:
        raise NodeExecutionError(exc.error_code, "上传视频增稳失败，请确认视频可解析且 ffmpeg 支持 vidstab。") from exc
    stored = store_file(object_store(request), output_path, purpose="media-stabilized")
    updates = {
        "object_uri": stored.ref.uri,
        "sha256": stored.sha256,
        "size_bytes": stored.size_bytes,
        "stabilized": True,
    }
    upload = upload_repository(request).patch_upload(upload.id, updates)
    return upload, media_info


def _normalize_upload_video(
    request: Request, upload: c.UploadSession
) -> tuple[c.UploadSession, c.MediaInfo]:
    """Normalize a portrait/b-roll upload to the strict delivery profile.

    Rotation correction, optional letterbox crop, HDR->SDR(bt709) tonemap, 1080p
    scale/pad, h264/yuv420p, and a post-encode validate gate that raises on any
    profile violation (Spec §2.3 no-silent-degrade). Replaces the upload object
    with the normalized asset so downstream stabilize/probe/thumbnail see upright,
    correctly-colored, profile-conformant media."""
    if upload.object_uri is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Upload object is missing.")
    source_path = local_object_path(object_store(request), upload.object_uri)
    try:
        result = normalize_for_upload(source_path)
    except FfmpegCommandError as exc:
        raise NodeExecutionError(
            exc.error_code,
            "上传视频规范化失败，请确认视频可解析且符合受支持的格式。",
        ) from exc
    stored = store_file(object_store(request), result.output_path, purpose="media-normalized")
    # ``normalized`` is informational only and has no DB column (no migration in
    # this cluster), so it is not persisted via patch — we stamp it on the
    # returned contract so the response/tags reflect it.
    updates = {
        "object_uri": stored.ref.uri,
        "sha256": stored.sha256,
        "size_bytes": stored.size_bytes,
    }
    upload = upload_repository(request).patch_upload(upload.id, updates)
    return upload.model_copy(update={"normalized": True}), result.media_info


def _create_upload_thumbnails(request: Request, artifact: c.Artifact) -> None:
    if artifact.uri is None or artifact.media_info is None or artifact.media_info.media_type != "video":
        return
    source_path = local_object_path(object_store(request), artifact.uri)
    try:
        thumbnails = extract_thumbnails(source_path, source_path.parent / f"{source_path.stem}_thumbs")
    except FfmpegCommandError as exc:
        raise NodeExecutionError(exc.error_code, "Uploaded media thumbnail extraction failed.") from exc
    for thumbnail in thumbnails:
        ref = object_store(request).prepare_upload(thumbnail.path.name, "thumbnails")
        stored = object_store(request).put_bytes(ref, thumbnail.path.read_bytes())
        payload = {
            "source_artifact_id": artifact.id,
            "thumbnail_label": thumbnail.label,
        }
        upload_repository(request).create_artifact(
            kind=c.ArtifactKind.cover_image,
            payload_schema="uri-only",
            payload=payload,
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=thumbnail.media_info,
        )


def cancel_upload(upload_session_id: str, request: Request) -> c.UploadSession:
    upload = _load_upload(request, upload_session_id)
    # The browser may already have PUT the staging object; drop it on cancel.
    if upload.object_uri and upload.status in {c.UploadStatus.prepared, c.UploadStatus.uploading}:
        _safe_delete(object_store(request), upload.object_uri)
    return _patch_upload(request, upload_session_id, {"status": c.UploadStatus.cancelled})


def get_upload(upload_session_id: str, request: Request) -> c.UploadSession:
    upload = upload_repository(request).get_upload(upload_session_id)
    if upload is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
    return upload
