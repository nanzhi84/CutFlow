from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import FileResponse

from apps.api.common import (
    media_repository,
    object_store,
    provider_repository,
    request_id,
    signed,
)
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError
from apps.api.services import annotation_batch as annotation_batch_service
from apps.api.services import asset_annotation, media_processing
from packages.media.assets import local_object_path

_PLAYABLE_MEDIA_TYPES = {"video", "audio"}


def _content_type_for(uri: str | None, media_info: c.MediaInfo | None) -> str | None:
    # Prefer the probed mime, then fall back to the object extension. Never raise:
    # an unknown content type is legal (the player just treats it as opaque).
    if media_info is not None and media_info.mime_type:
        return media_info.mime_type
    if uri:
        guessed = mimetypes.guess_type(Path(urlsplit(uri).path).name)[0]
        if guessed:
            return guessed
    return None


def _playable_for(media_info: c.MediaInfo | None, content_type: str | None) -> bool:
    if media_info is not None:
        return media_info.media_type in _PLAYABLE_MEDIA_TYPES
    if content_type:
        return content_type.split("/", 1)[0] in _PLAYABLE_MEDIA_TYPES
    return False


def _with_preview_playback(
    response: c.SignedUrlResponse, uri: str | None, media_info: c.MediaInfo | None
) -> c.SignedUrlResponse:
    content_type = _content_type_for(uri, media_info)
    return response.model_copy(
        update={
            "request_id": request_id(),
            "content_type": content_type,
            "playable": _playable_for(media_info, content_type),
        }
    )


def _with_browser_preview_url(
    asset_id: str,
    response: c.SignedUrlResponse,
    uri: str | None,
    media_info: c.MediaInfo | None,
) -> c.SignedUrlResponse:
    response = _with_preview_playback(response, uri, media_info)
    if response.url.startswith("local://"):
        return response.model_copy(update={"url": f"/api/media/assets/{asset_id}/content"})
    return response


def _source_for_asset(request: Request, asset_id: str) -> tuple[str, c.MediaInfo | None] | None:
    return media_repository(request).media_source_for_asset(asset_id)


def list_media_assets(
    request: Request,
    limit: int = 50,
    case_id: str | None = None,
    kind: str | None = None,
    annotation_status: str | None = None,
) -> c.PageResponse[c.MediaAssetCard]:
    values = media_repository(request).list_assets(
        limit=limit,
        case_id=case_id,
        kind=kind,
        annotation_status=annotation_status,
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def material_usage_ranking(
    request: Request,
    kind: c.SelectionMedium,
    case_id: str | None = None,
    top_n: int = 20,
) -> c.MaterialUsageRankingReport:
    report = media_repository(request).material_usage_ranking(
        kind=kind,
        case_id=case_id,
        top_n=top_n,
    )
    return report.model_copy(update={"request_id": request_id()})


def create_media_asset(payload: c.CreateMediaAssetFromUploadRequest, request: Request) -> c.MediaAssetRecord:
    return media_repository(request).create_asset_from_upload(payload)


def media_asset_detail(request: Request, asset_id: str) -> c.MediaAssetDetail:

    detail = media_repository(request).get_asset_detail(asset_id)
    if detail is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    return detail


def media_asset_preview(request: Request, asset_id: str) -> c.SignedUrlResponse:
    source = _source_for_asset(request, asset_id)
    if source is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    uri, media_info = source
    if uri:
        return _with_browser_preview_url(
            asset_id,
            object_store(request).signed_url(uri),
            uri,
            media_info,
        )
    return _with_preview_playback(signed(request, f"media/{asset_id}"), None, None)


def media_asset_content(request: Request, asset_id: str) -> FileResponse:
    source = _source_for_asset(request, asset_id)
    if source is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    uri, media_info = source
    if not uri.startswith("local://"):
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Local media object missing.")
    try:
        path = local_object_path(object_store(request), uri)
    except (ValueError, OSError) as exc:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Local media object missing.") from exc
    if not path.exists():
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Local media object missing.")
    return FileResponse(
        path,
        media_type=_content_type_for(uri, media_info),
        filename=Path(urlsplit(uri).path).name,
        content_disposition_type="inline",
    )


def delete_media_asset(request: Request, asset_id: str) -> c.OkResponse:
    """Delete a media-asset registration (e.g. a retired ``cover_template``). The
    backing source artifact/object is intentionally retained — artifacts are
    append-only and may be referenced by prior runs."""
    if not media_repository(request).delete_asset(asset_id):
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    return c.OkResponse(request_id=request_id())


def batch_stabilize_assets(
    payload: c.BatchStabilizeMediaAssetsRequest, request: Request
) -> c.BatchMediaProcessResponse:
    return media_processing.batch_stabilize_assets(payload, request)


def replace_asset_source(
    asset_id: str, payload: c.MediaAssetReplaceSourceRequest, request: Request
) -> c.MediaAssetReplaceResponse:
    return media_processing.replace_asset_source(asset_id, payload, request)


def auto_match_replace(
    payload: c.AutoMatchReplaceRequest, request: Request
) -> c.AutoMatchReplaceResponse:
    return media_processing.auto_match_replace(payload, request)


def trim_annotation(
    asset_id: str, payload: c.TrimAnnotationRequest, request: Request
) -> c.TrimAnnotationResponse:
    return media_processing.trim_annotation(asset_id, payload, request)


def get_annotation(request: Request, asset_id: str) -> c.AnnotationEditorVm:

    editor = media_repository(request).get_or_create_annotation(asset_id)
    if editor is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    return editor


def patch_annotation(asset_id: str, payload: c.PatchAnnotationRequest, request: Request) -> c.AnnotationEditorVm:
    editor = media_repository(request).patch_annotation(asset_id, payload)
    if editor is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    return editor


def batch_annotation(
    payload: c.AnnotationBatchRequest, request: Request
) -> c.AnnotationBatchResponse:
    return annotation_batch_service.run_batch_annotation(payload, request)


def rerun_annotation(
    asset_id: str, payload: c.RerunAnnotationRequest, request: Request
) -> c.AnnotationRunResponse:
    media_repo = media_repository(request)
    # Production (DB) path: drive the gated sensors + (gated) VLM -> AnnotationV4
    # pipeline, persisting a real AnnotationV4 canonical so material planning reads it
    # (Spec §12.2). Without a real vlm.annotation profile it degrades to a sensor-only
    # vlm_unconfigured result (never fabricated semantics).
    if payload.provider_profile_id:
        # BGM/audio assets are annotated through the gated audio.understanding path;
        # everything else through vlm.annotation. Validate the explicit profile's
        # capability against the asset's annotation path so a correct profile isn't rejected.
        db_asset = media_repo.asset_record(asset_id)
        expected_capability = (
            "audio.understanding"
            if (db_asset is not None and db_asset.kind == "bgm")
            else "vlm.annotation"
        )
        provider_repo = provider_repository(request)
        profile = next(
            (
                p
                for p in provider_repo.list_profiles(capability=expected_capability, limit=100)
                if p.id == payload.provider_profile_id
            ),
            None,
        )
        if profile is None:
            raise NodeExecutionError(
                c.ErrorCode.provider_unsupported_option, "Annotation provider profile is invalid."
            )
    response = asset_annotation.run_sqlalchemy_asset_annotation(request, asset_id, payload)
    if response is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    return response
