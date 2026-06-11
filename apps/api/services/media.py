from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Request, Response, UploadFile
from fastapi.responses import JSONResponse

from apps.api.common import (
    auth,
    case_learning_repository,
    case_repository,
    ensure_artifact_ref,
    get_case,
    media_repository,
    object_store,
    ops_repository,
    page,
    production_repository,
    prompt_repository,
    provider_repository,
    publishing_repository,
    repository,
    request_id,
    secret_repository,
    secret_store,
    signed,
    upload_repository,
    workflow_runtime,
)
from apps.api.dependencies import SESSION_COOKIE, current_user, not_found_response
from packages.core import contracts as c
from packages.core.auth import SqlAlchemyAuthService
from packages.core.contracts.state_machines import assert_transition
from packages.core.observability import metric_snapshot
from packages.core.registration_codes import hash_registration_code
from packages.core.storage.object_store import parse_local_uri
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.ai.gateway import ProviderCall

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
    updated = editor.model_copy(update={"etag": new_id("etag")})
    repository(request).annotations[asset_id] = updated
    repository(request).media_assets[asset_id] = repository(request).media_assets[asset_id].model_copy(
        update={"annotation_status": "annotated", "updated_at": c.utcnow()}
    )
    return updated


def rerun_annotation(
    asset_id: str, payload: c.RerunAnnotationRequest, request: Request
) -> c.AnnotationRunResponse:
    if media_repository(request) is not None:
        response = media_repository(request).rerun_annotation(asset_id, payload)
        if response is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return response
    if payload.provider_profile_id:
        repo = repository(request)
        asset = repo.media_assets[asset_id]
        profile = repo.provider_profiles.get(payload.provider_profile_id)
        if profile is None or profile.capability != "vlm.annotation":
            raise NodeExecutionError(c.ErrorCode.provider_unsupported_option, "Annotation provider profile is invalid.")
        source_uri = ""
        if asset.source_artifact_id and asset.source_artifact_id in repo.artifacts:
            source_uri = repo.artifacts[asset.source_artifact_id].uri or ""
        prompt_invocation, rendered = request.app.state.prompt_registry.render(
            node_id="MediaAssetAnnotation",
            variables={"asset_id": asset.id, "asset_kind": asset.kind},
            case_id=asset.case_id,
            provider_profile_id=profile.id,
        )
        invocation, result = request.app.state.provider_gateway.invoke(
            ProviderCall(
                case_id=asset.case_id,
                provider_profile_id=profile.id,
                capability_id="vlm.annotation",
                prompt_version_id=prompt_invocation.prompt_version_id,
                input={
                    "asset_id": asset.id,
                    "asset_kind": asset.kind,
                    "asset_uri": source_uri,
                    "prompt": rendered,
                },
            )
        )
        if result is None or invocation.error:
            repo.media_assets[asset_id] = asset.model_copy(
                update={"annotation_status": "annotation_failed", "usable": False, "updated_at": c.utcnow()}
            )
            return c.AnnotationRunResponse(asset_id=asset_id, run_id=None, status="failed")
        repo.prompt_invocations[prompt_invocation.id] = prompt_invocation.model_copy(
            update={"provider_invocation_id": invocation.id, "updated_at": c.utcnow()}
        )
        canonical = result.output.get("canonical")
        if not isinstance(canonical, dict):
            canonical = {"labels": asset.tags, "kind": asset.kind}
        usable = bool((canonical.get("quality") or {}).get("valid", True)) if isinstance(canonical.get("quality"), dict) else True
        repo.annotations[asset_id] = c.AnnotationEditorVm(
            asset=asset,
            etag=new_id("etag"),
            canonical=canonical,
            projection={"title": asset.title, "usable": usable},
            editable_paths=["/labels", "/usable", "/title"],
        )
        repo.media_assets[asset_id] = asset.model_copy(
            update={"annotation_status": "annotated", "usable": usable, "updated_at": c.utcnow()}
        )
        return c.AnnotationRunResponse(asset_id=asset_id, run_id=None, status="completed")
    repository(request).media_assets[asset_id] = repository(request).media_assets[asset_id].model_copy(
        update={"annotation_status": "annotated", "updated_at": c.utcnow()}
    )
    return c.AnnotationRunResponse(asset_id=asset_id, run_id=None, status="completed")
