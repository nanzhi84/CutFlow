from __future__ import annotations

import logging
import mimetypes
import re
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse

from apps.api.common import (
    assert_owner_or_404,
    finished_video_owner,
    object_store,
    production_repository,
    request_id,
    run_owner,
    signed,
    visible_owner_filter,
)
from apps.api.dependencies import current_user
from packages.core import contracts as c
from packages.core.storage.database import ArtifactRow, CaseRow
from packages.core.storage.object_store import parse_object_uri
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.media.assets import local_object_path, store_file
from packages.media.video import FfmpegCommandError, extract_frame_at_time
from packages.production.sqlalchemy_mappers import artifact_ref_from_row, artifact_row_to_contract


_BROWSER_DOWNLOAD_PREFIXES = ("http://", "https://", "/")
_VIDEO_PROXY_EXPIRES_IN = timedelta(minutes=15)
_VIDEO_ARTIFACT_KINDS = {
    c.ArtifactKind.video_portrait_track,
    c.ArtifactKind.video_lipsync,
    c.ArtifactKind.video_rendered,
    c.ArtifactKind.video_final,
    c.ArtifactKind.video_finished,
}
logger = logging.getLogger(__name__)


def performance_attribution(request: Request, video_version_id: str) -> c.PerformanceAttributionResponse:
    attribution = production_repository(request).performance_attribution(video_version_id)
    if attribution is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Video version is missing.")
    return attribution


def case_finished_videos(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.FinishedVideo]:
    # Creator-based isolation (spec §3): operator/viewer only see their own finished
    # videos; admin (owner_filter is None) sees all rows.
    owner_filter = visible_owner_filter(current_user(request))
    values = production_repository(request).list_finished_videos(
        case_id=case_id, limit=limit, owner_user_id=owner_filter
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def case_finished_video_downloads(
    request: Request, case_id: str, ids: str | None
) -> c.BatchFinishedVideoDownloadResponse:
    wanted_ids = [item.strip() for item in (ids or "").split(",") if item.strip()]
    if wanted_ids:
        finished_videos = [_finished_video_for_package(request, finished_id) for finished_id in wanted_ids]
    else:
        owner_filter = visible_owner_filter(current_user(request))
        finished_videos = production_repository(request).list_finished_videos(
            case_id=case_id,
            limit=100,
            owner_user_id=owner_filter,
        )
    items: list[c.FinishedVideoDownloadItem] = []
    for finished in finished_videos:
        if finished.case_id != case_id:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
        assert_owner_or_404(current_user(request), finished_video_owner(request, finished.id))
        artifact = _artifact_for_download(request, finished.video_artifact.artifact_id)
        if artifact is None or not artifact.uri:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video artifact is missing.")
        download_url, expires_at = _browser_download_fields(
            request,
            artifact.id,
            artifact.uri,
            disposition="attachment",
        )
        filename = _safe_package_name(finished.title or finished.id)
        suffix = _artifact_suffix(finished.video_artifact, ".mp4")
        items.append(
            c.FinishedVideoDownloadItem(
                finished_video_id=finished.id,
                title=finished.title,
                url=download_url,
                expires_at=expires_at,
                content_type=_artifact_download_content_type(artifact),
                filename=f"{filename}{suffix}",
            )
        )
    return c.BatchFinishedVideoDownloadResponse(items=items, request_id=request_id())


def finished_video_detail(request: Request, id: str) -> c.FinishedVideoDetail:
    assert_owner_or_404(current_user(request), finished_video_owner(request, id))
    detail = production_repository(request).finished_video_detail(id)
    if detail is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
    return detail


def finished_video_preview(request: Request, id: str) -> c.SignedUrlResponse:
    assert_owner_or_404(current_user(request), finished_video_owner(request, id))
    uri = production_repository(request).artifact_uri_for_finished_video(id)
    if uri is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
    if uri:
        if _browser_proxyable_uri(uri):
            return _finished_video_proxy_url(request, id, uri)
        return object_store(request).signed_url(uri).model_copy(update={"request_id": request_id()})
    return signed(request, f"finished-videos/{id}/preview.mp4")


def finished_video_download(request: Request, id: str) -> c.SignedUrlResponse:
    assert_owner_or_404(current_user(request), finished_video_owner(request, id))
    finished = _finished_video_for_package(request, id)
    package_artifact = _build_finished_video_publish_package(request, finished)
    download_url, expires_at = _browser_download_fields(
        request,
        package_artifact.artifact_id,
        package_artifact.uri,
    )
    return c.SignedUrlResponse(
        url=download_url,
        expires_at=expires_at,
        request_id=request_id(),
        content_type="application/zip",
        playable=False,
    )


def finished_video_stream(request: Request, id: str) -> FileResponse:
    assert_owner_or_404(current_user(request), finished_video_owner(request, id))
    uri = _finished_video_uri(request, id)
    if uri is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
    if not _browser_proxyable_uri(uri):
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is not streamable.")
    try:
        path = local_object_path(object_store(request), uri)
    except Exception as exc:
        logger.warning("Failed to resolve finished video %s at %s.", id, uri, exc_info=True)
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is not readable.") from exc
    if not path.exists():
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is not readable.")
    return FileResponse(
        path,
        media_type=_video_content_type(uri),
        filename=Path(urlsplit(uri).path).name or f"{id}.mp4",
        content_disposition_type="inline",
    )


def _finished_video_for_package(request: Request, finished_video_id: str) -> c.FinishedVideo:
    detail = production_repository(request).finished_video_detail(finished_video_id)
    if detail is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
    return detail.finished_video


def _build_finished_video_publish_package(
    request: Request,
    finished: c.FinishedVideo,
) -> c.ArtifactRef:
    with tempfile.TemporaryDirectory(prefix="cutagent-publish-package-") as directory:
        work_dir = Path(directory)
        video_path = _download_ref_for_package(
            request,
            finished.video_artifact,
            work_dir / f"video{_artifact_suffix(finished.video_artifact, '.mp4')}",
        )
        cover_path = _cover_path_for_package(request, finished, video_path, work_dir)
        title_path = work_dir / "title.txt"
        title_path.write_text(f"{finished.title.strip() or finished.id}\n", encoding="utf-8")

        package_name = f"{_safe_package_name(finished.title or finished.id)}_publish_package.zip"
        zip_path = work_dir / package_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(title_path, "title.txt")
            archive.write(cover_path, f"cover{cover_path.suffix or '.jpg'}")
            archive.write(video_path, f"video{video_path.suffix or '.mp4'}")

        stored = store_file(object_store(request), zip_path, purpose="publish-packages")
        payload = {
            "finished_video_id": finished.id,
            "title": finished.title,
            "files": {
                "title": "title.txt",
                "cover": f"cover{cover_path.suffix or '.jpg'}",
                "video": f"video{video_path.suffix or '.mp4'}",
            },
            "portable_resources": True,
        }
        return _create_publish_package_artifact(
            request,
            finished=finished,
            uri=stored.ref.uri,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            payload=payload,
        )


def _download_ref_for_package(request: Request, artifact_ref: c.ArtifactRef, target: Path) -> Path:
    uri = _artifact_uri_for_ref(request, artifact_ref)
    try:
        return object_store(request).download_file(parse_object_uri(uri), target)
    except (ValueError, OSError) as exc:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publish package asset is not readable.") from exc


def _cover_path_for_package(
    request: Request,
    finished: c.FinishedVideo,
    video_path: Path,
    work_dir: Path,
) -> Path:
    if finished.cover_artifact is not None:
        return _download_ref_for_package(
            request,
            finished.cover_artifact,
            work_dir / f"cover{_artifact_suffix(finished.cover_artifact, '.jpg')}",
        )
    try:
        return extract_frame_at_time(video_path, work_dir / "cover.png", time_sec=0).path
    except FfmpegCommandError as exc:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video cover is missing.") from exc


def _artifact_uri_for_ref(request: Request, artifact_ref: c.ArtifactRef) -> str:
    artifact = _artifact_for_download(request, artifact_ref.artifact_id)
    if artifact is not None and artifact.uri:
        return artifact.uri
    if artifact_ref.uri and not artifact_ref.uri.startswith("artifact://"):
        return artifact_ref.uri
    raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact URI is missing.")


def _artifact_suffix(artifact_ref: c.ArtifactRef, fallback: str) -> str:
    suffix = Path(urlsplit(artifact_ref.uri or "").path).suffix.lower()
    if suffix and re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    return fallback


def _safe_package_name(title: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\\s]+", "_", title.strip()).strip("._")
    return (cleaned or "finished_video")[:80]


def _create_publish_package_artifact(
    request: Request,
    *,
    finished: c.FinishedVideo,
    uri: str,
    sha256: str,
    size_bytes: int,
    payload: dict,
) -> c.ArtifactRef:
    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    with session_factory() as session:
        artifact = ArtifactRow(
            id=new_id("art"),
            case_id=finished.case_id,
            run_id=finished.run_id,
            kind=c.ArtifactKind.publish_package.value,
            uri=uri,
            size_bytes=size_bytes,
            sha256=sha256,
            payload_schema="FinishedVideoPublishPackageArtifact.v1",
            payload=payload,
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact_ref_from_row(artifact)


def latest_jianying_draft(id: str, request: Request) -> c.LatestJianyingDraftPackageResponse:
    assert_owner_or_404(current_user(request), finished_video_owner(request, id))
    latest = production_repository(request).latest_jianying_draft(id)
    package = _with_browser_download_url(request, latest) if latest is not None else None
    return c.LatestJianyingDraftPackageResponse(package=package, request_id=request_id())


def delete_finished_video(id: str, request: Request, reason: str | None = None) -> c.OkResponse:
    case_id = _finished_video_case_id_db(request, id)
    if case_id is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
    _record_discard_reward(request, case_id, id, reason)
    if not production_repository(request).delete_finished_video(id):
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
    return c.OkResponse(request_id=request_id())


def _finished_video_case_id_db(request: Request, finished_video_id: str) -> str | None:
    detail = production_repository(request).finished_video_detail(finished_video_id)
    return detail.finished_video.case_id if detail is not None else None


def _record_discard_reward(
    request: Request, case_id: str | None, finished_video_id: str, reason: str | None
) -> None:
    """Reward搭车: emit a video_discarded RewardSignal before deletion (§5.2). The
    reason drives the value (only ``script`` is a negative signal). Best-effort: the
    learning layer must never block the existing delete flow."""
    if case_id is None:
        return
    from apps.api.services import case_rubric

    try:
        case_rubric.record_discard_reward(request, case_id, finished_video_id, reason)
    except Exception:  # pragma: no cover - learning side-channel is best-effort
        pass


def editor_handoff(
    id: str, payload: c.CreateEditorHandoffRequest, request: Request
) -> c.EditorHandoffPackageArtifact:
    assert_owner_or_404(current_user(request), finished_video_owner(request, id))
    return production_repository(request).create_editor_handoff(id, payload)


def jianying_draft(
    id: str, payload: c.CreateJianyingDraftRequest, request: Request
) -> c.JianyingDraftPackageArtifact:
    assert_owner_or_404(current_user(request), finished_video_owner(request, id))
    return _with_browser_download_url(
        request, production_repository(request).create_jianying_draft(id, payload)
    )


def artifact_download(
    request: Request, artifact_id: str, disposition: str | None = None
) -> FileResponse | RedirectResponse:
    artifact = _artifact_for_download(request, artifact_id)
    if artifact is None or not artifact.uri:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact is missing.")
    _assert_package_download_allowed(request, artifact)

    signed_url = object_store(request).signed_url(artifact.uri).url
    if signed_url.startswith(("http://", "https://")):
        return RedirectResponse(signed_url)
    try:
        path = local_object_path(object_store(request), artifact.uri)
    except (ValueError, OSError) as exc:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact is not locally readable.") from exc
    if not path.exists():
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact is not locally readable.")
    return FileResponse(
        path,
        media_type=_artifact_download_content_type(artifact),
        filename=Path(urlsplit(artifact.uri).path).name or f"{artifact.id}.zip",
        content_disposition_type=_artifact_download_disposition(artifact, disposition),
    )


def _finished_video_uri(request: Request, finished_video_id: str) -> str | None:
    return production_repository(request).artifact_uri_for_finished_video(finished_video_id)


def _browser_proxyable_uri(uri: str) -> bool:
    # Only ``local://`` filesystem objects need the same-origin ``/stream`` proxy:
    # they have no browser-reachable URL. ``s3://`` (incl. Aliyun OSS) is served via
    # a presigned HTTPS URL the browser streams directly from the bucket/CDN — that
    # keeps native HTTP range (scrubbing) and avoids a blocking download-through the
    # API server. Proxying ``s3://`` here would force the API to pull the whole
    # object into its cache before responding, so it stays on the signed-URL path.
    return uri.startswith("local://")


def _video_content_type(uri: str) -> str:
    guessed = mimetypes.guess_type(Path(urlsplit(uri).path).name)[0]
    return guessed or "video/mp4"


def _finished_video_proxy_url(request: Request, finished_video_id: str, uri: str) -> c.SignedUrlResponse:
    return c.SignedUrlResponse(
        url=f"/api/finished-videos/{finished_video_id}/stream",
        expires_at=c.utcnow() + _VIDEO_PROXY_EXPIRES_IN,
        request_id=request_id(),
        content_type=_video_content_type(uri),
        playable=True,
    )


def _with_browser_download_url(
    request: Request, result: c.JianyingDraftPackageArtifact
) -> c.JianyingDraftPackageArtifact:
    package_uri = result.package_artifact.uri
    download_url, download_expires_at = _browser_download_fields(
        request, result.package_artifact.artifact_id, package_uri
    )
    return result.model_copy(
        update={"download_url": download_url, "download_expires_at": download_expires_at}
    )


def _browser_download_fields(
    request: Request,
    artifact_id: str,
    uri: str,
    *,
    disposition: str | None = None,
) -> tuple[str, object]:
    signed_url = object_store(request).signed_url(
        uri, response_content_disposition=disposition
    )
    url = signed_url.url
    if not url.startswith(_BROWSER_DOWNLOAD_PREFIXES):
        url = f"/api/artifacts/{artifact_id}/download"
        if disposition:
            url = f"{url}?disposition={disposition}"
    return url, signed_url.expires_at


def _artifact_for_download(request: Request, artifact_id: str) -> c.Artifact | None:
    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    with session_factory() as session:
        row = session.get(ArtifactRow, artifact_id)
        return artifact_row_to_contract(row) if row is not None else None


def _assert_package_download_allowed(request: Request, artifact: c.Artifact) -> None:
    if artifact.kind == c.ArtifactKind.cover_image:
        _assert_cover_download_allowed(request, artifact)
        return
    if _is_video_download_artifact(artifact):
        _assert_video_download_allowed(request, artifact)
        return
    if artifact.kind not in {
        c.ArtifactKind.jianying_draft,
        c.ArtifactKind.editor_handoff,
        c.ArtifactKind.publish_package,
    }:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact is not downloadable.")
    payload = artifact.payload if isinstance(artifact.payload, dict) else {}
    finished_video_id = payload.get("finished_video_id")
    if not isinstance(finished_video_id, str) or not finished_video_id:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact is not downloadable.")
    assert_owner_or_404(current_user(request), finished_video_owner(request, finished_video_id))


def _assert_cover_download_allowed(request: Request, artifact: c.Artifact) -> None:
    user = current_user(request)
    if user.role == c.UserRole.admin:
        return
    if not artifact.case_id:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact is not downloadable.")
    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    with session_factory() as session:
        case = session.get(CaseRow, artifact.case_id)
        assert_owner_or_404(user, case.owner_user_id if case is not None else None)
        return


def _is_video_download_artifact(artifact: c.Artifact) -> bool:
    if artifact.kind in _VIDEO_ARTIFACT_KINDS:
        return True
    if artifact.kind != c.ArtifactKind.uploaded_file:
        return False
    payload = artifact.payload if isinstance(artifact.payload, dict) else {}
    content_type = payload.get("content_type")
    upload_kind = payload.get("kind")
    return (
        getattr(artifact.media_info, "media_type", None) == "video"
        or (isinstance(content_type, str) and content_type.startswith("video/"))
        or upload_kind in {"publish_video", "video", "portrait", "broll"}
    )


def _assert_video_download_allowed(request: Request, artifact: c.Artifact) -> None:
    user = current_user(request)
    if user.role == c.UserRole.admin:
        return
    payload = artifact.payload if isinstance(artifact.payload, dict) else {}
    finished_video_id = payload.get("finished_video_id")
    if isinstance(finished_video_id, str) and finished_video_id:
        assert_owner_or_404(user, finished_video_owner(request, finished_video_id))
        return
    if artifact.run_id:
        assert_owner_or_404(user, run_owner(request, artifact.run_id))
        return
    case_id = artifact.case_id or (payload.get("case_id") if isinstance(payload.get("case_id"), str) else None)
    if case_id:
        assert_owner_or_404(user, _case_owner_user_id(request, case_id))
        return
    raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact is not downloadable.")


def _case_owner_user_id(request: Request, case_id: str) -> str | None:
    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    with session_factory() as session:
        case = session.get(CaseRow, case_id)
        return case.owner_user_id if case is not None else None


def _artifact_download_content_type(artifact: c.Artifact) -> str:
    if artifact.kind == c.ArtifactKind.cover_image:
        return mimetypes.guess_type(Path(urlsplit(artifact.uri or "").path).name)[0] or "image/jpeg"
    if _is_video_download_artifact(artifact):
        return (
            getattr(artifact.media_info, "mime_type", None)
            or mimetypes.guess_type(Path(urlsplit(artifact.uri or "").path).name)[0]
            or "video/mp4"
        )
    return "application/zip"


def _artifact_download_disposition(artifact: c.Artifact, requested: str | None = None) -> str:
    if requested == "attachment":
        return "attachment"
    return "inline" if artifact.kind == c.ArtifactKind.cover_image or _is_video_download_artifact(artifact) else "attachment"
