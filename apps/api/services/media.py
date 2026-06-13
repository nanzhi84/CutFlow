from __future__ import annotations


from fastapi import Request

from apps.api.common import (
    media_repository,
    object_store,
    page,
    repository,
    request_id,
    signed,
)
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from apps.api.services import asset_annotation, media_processing

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
        uri = media_repository(request).artifact_uri_for_asset(asset_id)
        if uri is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        if uri:
            return object_store(request).signed_url(uri).model_copy(update={"request_id": request_id()})
        return signed(request, f"media/{asset_id}")
    if asset_id not in repository(request).media_assets:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    asset = repository(request).media_assets[asset_id]
    if asset.source_artifact_id and asset.source_artifact_id in repository(request).artifacts:
        artifact = repository(request).artifacts[asset.source_artifact_id]
        if artifact.uri:
            return object_store(request).signed_url(artifact.uri).model_copy(update={"request_id": request_id()})
    return signed(request, f"media/{asset_id}")


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
    canonical = dict(editor.canonical or {})
    projection = dict(editor.projection or {})
    _apply_annotation_operations(canonical, projection, payload.patch.operations)
    updated = editor.model_copy(update={"etag": new_id("etag"), "canonical": canonical, "projection": projection})
    repository(request).annotations[asset_id] = updated
    repository(request).media_assets[asset_id] = repository(request).media_assets[asset_id].model_copy(
        update={"annotation_status": "annotated", "updated_at": c.utcnow()}
    )
    return updated


def _apply_annotation_operations(canonical: dict, projection: dict, operations: list[dict]) -> None:
    for operation in operations:
        op_name = operation.get("op", "replace")
        path = operation.get("path")
        if op_name not in {"add", "replace"} or not isinstance(path, str) or "value" not in operation:
            continue
        value = operation["value"]
        if path == "/labels":
            canonical["labels"] = value
        elif path == "/usable":
            projection["usable"] = value
        elif path == "/title":
            projection["title"] = value
        elif path.startswith("/canonical/"):
            _set_nested(canonical, path.removeprefix("/canonical/").split("/"), value)
        elif path.startswith("/projection/"):
            _set_nested(projection, path.removeprefix("/projection/").split("/"), value)


def _set_nested(target: dict, parts: list[str], value) -> None:
    current = target
    for part in [item for item in parts[:-1] if item]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    if parts and parts[-1]:
        current[parts[-1]] = value


def rerun_annotation(
    asset_id: str, payload: c.RerunAnnotationRequest, request: Request
) -> c.AnnotationRunResponse:
    if media_repository(request) is not None:
        response = media_repository(request).rerun_annotation(asset_id, payload)
        if response is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return response
    repo = repository(request)
    if asset_id not in repo.media_assets:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    if payload.provider_profile_id:
        profile = repo.provider_profiles.get(payload.provider_profile_id)
        if profile is None or profile.capability != "vlm.annotation":
            raise NodeExecutionError(
                c.ErrorCode.provider_unsupported_option, "Annotation provider profile is invalid."
            )
    # The gated runner drives the full sensors + (gated) VLM -> AnnotationV4 path,
    # persists the AnnotationV4 artifact, and projects it into the editor. Without a
    # real vlm.annotation profile + active secret it degrades to a sensor-only
    # vlm_unconfigured result (never fabricated semantics).
    return asset_annotation.run_inmemory_asset_annotation(request, asset_id, payload)
