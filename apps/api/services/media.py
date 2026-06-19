from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request

from apps.api.common import (
    media_repository,
    object_store,
    page,
    provider_repository,
    repository,
    request_id,
    signed,
)
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from apps.api.services import annotation_batch as annotation_batch_service
from apps.api.services import annotation_patch, asset_annotation, media_processing

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

def list_media_assets(
    request: Request,
    limit: int = 50,
    case_id: str | None = None,
    kind: str | None = None,
    annotation_status: str | None = None,
) -> c.PageResponse[c.MediaAssetCard]:
    if media_repository(request) is not None:
        values = media_repository(request).list_assets(
            limit=limit,
            case_id=case_id,
            kind=kind,
            annotation_status=annotation_status,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    assets = list(repository(request).media_assets.values())
    if case_id:
        assets = [asset for asset in assets if asset.case_id == case_id]
    if kind:
        assets = [asset for asset in assets if asset.kind == kind]
    if annotation_status:
        assets = [asset for asset in assets if asset.annotation_status == annotation_status]
    return page([c.MediaAssetCard(asset=asset, preview_url=f"local://media/{asset.id}") for asset in assets], limit)


def material_usage_ranking(
    request: Request,
    kind: c.SelectionMedium,
    case_id: str | None = None,
    top_n: int = 20,
) -> c.MaterialUsageRankingReport:
    if media_repository(request) is not None:
        report = media_repository(request).material_usage_ranking(
            kind=kind,
            case_id=case_id,
            top_n=top_n,
        )
    else:
        report = repository(request).material_usage_ranking(
            kind=kind,
            case_id=case_id,
            top_n=top_n,
        )
    return report.model_copy(update={"request_id": request_id()})


def create_media_asset(payload: c.CreateMediaAssetFromUploadRequest, request: Request) -> c.MediaAssetRecord:
    if media_repository(request) is not None:
        return media_repository(request).create_asset_from_upload(payload)
    upload = repository(request).uploads[payload.upload_session_id]
    if upload.status != c.UploadStatus.completed:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload must be completed first.")
    asset = c.MediaAssetRecord(
        id=new_id("asset"),
        case_id=payload.case_id,
        title=payload.title,
        kind=payload.kind,
        source_artifact_id=upload.id,
        tags=payload.tags,
    )
    repository(request).media_assets[asset.id] = asset
    return asset


def media_asset_detail(request: Request, asset_id: str) -> c.MediaAssetDetail:

    if media_repository(request) is not None:
        detail = media_repository(request).get_asset_detail(asset_id)
        if detail is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return detail
    asset = repository(request).media_assets[asset_id]
    return c.MediaAssetDetail(asset=asset, preview_url=f"local://media/{asset.id}")


def media_asset_preview(request: Request, asset_id: str) -> c.SignedUrlResponse:

    if media_repository(request) is not None:
        source = media_repository(request).media_source_for_asset(asset_id)
        if source is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        uri, media_info = source
        if uri:
            return _with_preview_playback(
                object_store(request).signed_url(uri), uri, media_info
            )
        return _with_preview_playback(signed(request, f"media/{asset_id}"), None, None)
    if asset_id not in repository(request).media_assets:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    asset = repository(request).media_assets[asset_id]
    if asset.source_artifact_id and asset.source_artifact_id in repository(request).artifacts:
        artifact = repository(request).artifacts[asset.source_artifact_id]
        if artifact.uri:
            return _with_preview_playback(
                object_store(request).signed_url(artifact.uri), artifact.uri, artifact.media_info
            )
    return _with_preview_playback(signed(request, f"media/{asset_id}"), None, None)


def delete_media_asset(request: Request, asset_id: str) -> c.OkResponse:
    """Delete a media-asset registration (e.g. a retired ``cover_template``). The
    backing source artifact/object is intentionally retained — artifacts are
    append-only and may be referenced by prior runs."""
    if media_repository(request) is not None:
        if not media_repository(request).delete_asset(asset_id):
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return c.OkResponse(request_id=request_id())
    if asset_id not in repository(request).media_assets:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    del repository(request).media_assets[asset_id]
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

    if media_repository(request) is not None:
        editor = media_repository(request).get_or_create_annotation(asset_id)
        if editor is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return editor
    asset = repository(request).media_assets[asset_id]
    if asset_id not in repository(request).annotations:
        repository(request).annotations[asset_id] = c.AnnotationEditorVm(
            asset=asset,
            etag=new_id("etag"),
            canonical={"labels": asset.tags, "kind": asset.kind},
            projection={"title": asset.title, "usable": asset.usable},
            editable_paths=["/labels", "/usable", "/title"],
        )
    return repository(request).annotations[asset_id]


def patch_annotation(asset_id: str, payload: c.PatchAnnotationRequest, request: Request) -> c.AnnotationEditorVm:
    if media_repository(request) is not None:
        editor = media_repository(request).patch_annotation(asset_id, payload)
        if editor is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return editor
    editor = get_annotation(request, asset_id)
    # canonical may be an AnnotationV4 (coerced on load) or the minimal editor dict;
    # normalize to a plain mutable dict so the PatchService can merge uniformly.
    raw_canonical = editor.canonical
    if isinstance(raw_canonical, c.AnnotationV4):
        canonical = raw_canonical.model_dump(mode="json")
    else:
        canonical = dict(raw_canonical or {})
    asset = repository(request).media_assets[asset_id]
    # PatchService merges structural edits (segments / quality_events) into the
    # canonical AnnotationV4 -> new canonical version, then rebuilds the projection
    # from canonical (Spec §12.1/§12.2). Invalid edits raise artifact.schema_mismatch (400).
    new_canonical, new_projection = annotation_patch.apply_patch(
        canonical=canonical,
        projection=dict(editor.projection or {}),
        asset=asset,
        operations=payload.patch.operations,
    )
    updated = editor.model_copy(
        update={"etag": new_id("etag"), "canonical": new_canonical, "projection": new_projection}
    )
    repository(request).annotations[asset_id] = updated
    repository(request).media_assets[asset_id] = asset.model_copy(
        update={"annotation_status": "annotated", "updated_at": c.utcnow()}
    )
    return updated


def batch_annotation(
    payload: c.AnnotationBatchRequest, request: Request
) -> c.AnnotationBatchResponse:
    return annotation_batch_service.run_batch_annotation(payload, request)


def rerun_annotation(
    asset_id: str, payload: c.RerunAnnotationRequest, request: Request
) -> c.AnnotationRunResponse:
    media_repo = media_repository(request)
    if media_repo is not None:
        # Production (DB) path: drive the SAME gated sensors + (gated) VLM -> AnnotationV4
        # pipeline as in-memory, persisting a real AnnotationV4 canonical so material
        # planning reads it (Spec §12.2). Without a real vlm.annotation profile it
        # degrades to a sensor-only vlm_unconfigured result (never fabricated semantics).
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
            profile = (
                next(
                    (
                        p
                        for p in provider_repo.list_profiles(capability=expected_capability, limit=100)
                        if p.id == payload.provider_profile_id
                    ),
                    None,
                )
                if provider_repo is not None
                else None
            )
            if profile is None:
                raise NodeExecutionError(
                    c.ErrorCode.provider_unsupported_option, "Annotation provider profile is invalid."
                )
        response = asset_annotation.run_sqlalchemy_asset_annotation(request, asset_id, payload)
        if response is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return response
    repo = repository(request)
    if asset_id not in repo.media_assets:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    if payload.provider_profile_id:
        profile = repo.provider_profiles.get(payload.provider_profile_id)
        # BGM/audio assets are annotated through the gated audio.understanding path;
        # everything else through vlm.annotation. Validate the explicit profile's
        # capability against the asset's annotation path so a correct profile isn't rejected.
        expected_capability = (
            "audio.understanding" if repo.media_assets[asset_id].kind == "bgm" else "vlm.annotation"
        )
        if profile is None or profile.capability != expected_capability:
            raise NodeExecutionError(
                c.ErrorCode.provider_unsupported_option, "Annotation provider profile is invalid."
            )
    # The gated runner drives the full sensors + (gated) VLM -> AnnotationV4 path,
    # persists the AnnotationV4 artifact, and projects it into the editor. Without a
    # real vlm.annotation profile + active secret it degrades to a sensor-only
    # vlm_unconfigured result (never fabricated semantics).
    return asset_annotation.run_inmemory_asset_annotation(request, asset_id, payload)
