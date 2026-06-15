from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import Request, UploadFile

from apps.api.common import (
    media_repository,
    object_store,
    publishing_repository,
    repository,
    request_id,
    settings,
    upload_repository,
)
from packages.core import contracts as c
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage.object_store import parse_object_uri
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


async def _stream_upload_to_disk(
    file: UploadFile,
    destination: Path,
    *,
    chunk_size: int,
    max_size_bytes: int | None,
) -> int:
    """Stream an UploadFile to ``destination`` in chunks.

    Bounds peak memory to one chunk (default 1 MiB) instead of buffering the
    whole — potentially hundreds-of-MB — file in RAM, and rejects an oversized
    body *early* (mid-stream) rather than after a full read. Returns the total
    bytes written. Raises ``upload.too_large`` once the running total exceeds
    ``max_size_bytes``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    await file.seek(0)
    with destination.open("wb") as handle:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if max_size_bytes is not None and total > max_size_bytes:
                handle.close()
                destination.unlink(missing_ok=True)
                raise NodeExecutionError(
                    c.ErrorCode.upload_too_large,
                    f"Uploaded file exceeds the maximum allowed size of {max_size_bytes} bytes.",
                )
            handle.write(chunk)
    return total

def prepare_upload(payload: c.PrepareUploadRequest, request: Request) -> c.UploadSession:
    object_ref = object_store(request).prepare_upload(payload.filename, payload.kind.value)
    upload = c.UploadSession(
        id=new_id("upl"),
        kind=payload.kind,
        case_id=payload.case_id,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        sha256=payload.sha256,
        upload_url=object_store(request).signed_url(object_ref.uri).url,
        object_uri=object_ref.uri,
        stabilize=payload.stabilize,
    )
    if upload_repository(request) is not None:
        return upload_repository(request).create_upload(upload)
    repository(request).uploads[upload.id] = upload
    return upload


async def upload_file(
    upload_session_id: str, request: Request, file: UploadFile | None = None
) -> c.UploadSession:
    if upload_repository(request) is not None:
        upload = upload_repository(request).get_upload(upload_session_id)
    else:
        upload = repository(request).uploads.get(upload_session_id)
    if upload is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
    if file is not None and upload.object_uri:
        upload_settings = settings(request).upload
        # Early hard ceiling: defence-in-depth against an oversized / under-declared
        # body. When the session declares a size we also cap at it (with one chunk
        # of slack) so the stream aborts before fully buffering an over-sized upload.
        ceiling = upload_settings.max_size_bytes
        if upload.size_bytes:
            ceiling = min(ceiling, upload.size_bytes + upload_settings.chunk_bytes)
        ref = parse_object_uri(upload.object_uri)
        # Stream the body to a temp file in chunks, then path-stream it to the
        # object store (S3 uses boto3 multipart from disk; Local copies by path).
        # Neither path buffers the whole object in RAM.
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="cutagent_upload_", suffix=f"_{ref.key.rsplit('/', 1)[-1]}")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            await _stream_upload_to_disk(
                file,
                tmp_path,
                chunk_size=max(1, upload_settings.chunk_bytes),
                max_size_bytes=ceiling,
            )
            stored = object_store(request).upload_file(tmp_path, ref)
        finally:
            tmp_path.unlink(missing_ok=True)
        if upload.size_bytes is not None and stored.size_bytes != upload.size_bytes:
            raise NodeExecutionError(c.ErrorCode.upload_size_mismatch, "Upload size mismatch.")
        if upload.sha256 and upload.sha256 != stored.sha256:
            raise NodeExecutionError(c.ErrorCode.upload_sha256_mismatch, "Upload sha256 mismatch.")
        updates = {"status": c.UploadStatus.uploading, "sha256": upload.sha256 or stored.sha256}
    else:
        updates = {"status": c.UploadStatus.uploading}
    if upload_repository(request) is not None:
        return upload_repository(request).patch_upload(upload_session_id, updates)
    assert_transition("upload_session", upload.status, updates["status"])
    return repository(request).patch(repository(request).uploads, upload_session_id, updates)


def complete_upload(payload: c.CompleteUploadRequest, request: Request) -> c.CompleteUploadResponse:
    upload = (
        upload_repository(request).get_upload(payload.upload_session_id)
        if upload_repository(request) is not None
        else repository(request).uploads[payload.upload_session_id]
    )
    if upload is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
    assert_transition("upload_session", upload.status, c.UploadStatus.completed)
    if payload.size_bytes is not None and payload.size_bytes != upload.size_bytes:
        raise NodeExecutionError(c.ErrorCode.upload_size_mismatch, "Upload size mismatch.")
    if upload.sha256 and payload.sha256 and upload.sha256 != payload.sha256:
        raise NodeExecutionError(c.ErrorCode.upload_sha256_mismatch, "Upload sha256 mismatch.")
    media_info = _probe_upload_media(request, upload)
    was_normalized = False
    is_av_video = (
        media_info is not None
        and media_info.media_type == "video"
        and upload.kind in {c.UploadKind.portrait, c.UploadKind.broll}
    )
    if is_av_video and settings(request).upload.normalize_video:
        upload, media_info = _normalize_upload_video(request, upload)
        was_normalized = True
    if upload.stabilize and upload.kind in {c.UploadKind.portrait, c.UploadKind.broll}:
        upload, media_info = _stabilize_upload_video(request, upload)
    if upload_repository(request) is not None:
        upload = upload_repository(request).patch_upload(upload.id, {"status": c.UploadStatus.completed})
        artifact = upload_repository(request).create_artifact_from_upload(upload, media_info=media_info)
        artifact_ref = upload_repository(request).artifact_ref(artifact.id)
    else:
        upload = repository(request).patch(repository(request).uploads, upload.id, {"status": c.UploadStatus.completed})
        artifact = repository(request).create_artifact(
            kind=c.ArtifactKind.uploaded_file,
            payload_schema="UploadedFileArtifact.v1",
            payload=upload.model_dump(mode="json"),
            uri=upload.object_uri,
            sha256=upload.sha256,
            media_info=media_info,
        )
        artifact_ref = repository(request).artifact_ref(artifact.id)
        _create_upload_thumbnails(request, artifact)
    media_asset = None
    publish_package = None
    replace_mode = payload.metadata.get("template_mode") == "replace"
    if upload.kind in {
        c.UploadKind.portrait,
        c.UploadKind.broll,
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
        if media_repository(request) is not None:
            media_asset = media_repository(request).create_asset_from_upload(media_payload)
        else:
            media_asset = c.MediaAssetRecord(
                id=new_id("asset"),
                case_id=media_payload.case_id,
                title=media_payload.title,
                kind=media_payload.kind,
                source_artifact_id=artifact.id,
                tags=media_payload.tags,
            )
            repository(request).media_assets[media_asset.id] = media_asset
    elif upload.kind == c.UploadKind.publish_video:
        package_payload = c.CreatePublishPackageRequest(
            upload_artifact_id=artifact.id,
            title=payload.metadata.get("title") or upload.filename,
            description=payload.metadata.get("description", ""),
        )
        if publishing_repository(request) is not None:
            publish_package = publishing_repository(request).create_package(package_payload)
        else:
            publish_package = c.PublishPackage(
                id=new_id("pkg"),
                case_id=upload.case_id,
                upload_artifact_id=artifact.id,
                video_artifact=artifact_ref,
                platform_defaults=c.PublishDefaults(
                    title=package_payload.title,
                    description=package_payload.description,
                ),
            )
            repository(request).publish_packages[publish_package.id] = publish_package
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
    if upload_repository(request) is not None:
        upload = upload_repository(request).patch_upload(upload.id, updates)
    else:
        upload = repository(request).patch(repository(request).uploads, upload.id, updates)
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
    if upload_repository(request) is not None:
        upload = upload_repository(request).patch_upload(upload.id, updates)
    else:
        upload = repository(request).patch(repository(request).uploads, upload.id, updates)
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
        repository(request).create_artifact(
            kind=c.ArtifactKind.cover_image,
            payload_schema="uri-only",
            payload={
                "source_artifact_id": artifact.id,
                "thumbnail_label": thumbnail.label,
            },
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=thumbnail.media_info,
        )


def cancel_upload(upload_session_id: str, request: Request) -> c.UploadSession:
    if upload_repository(request) is not None:
        return upload_repository(request).patch_upload(
            upload_session_id,
            {"status": c.UploadStatus.cancelled},
        )
    upload = repository(request).uploads[upload_session_id]
    assert_transition("upload_session", upload.status, c.UploadStatus.cancelled)
    return repository(request).patch(repository(request).uploads, upload_session_id, {"status": c.UploadStatus.cancelled})


def get_upload(upload_session_id: str, request: Request) -> c.UploadSession:
    if upload_repository(request) is not None:
        upload = upload_repository(request).get_upload(upload_session_id)
        if upload is None:
            raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
        return upload
    return repository(request).uploads[upload_session_id]
