from __future__ import annotations


from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import FileResponse

from apps.api.dependencies import require_role
from apps.api.services import media as service
from packages.core import contracts as c

router = APIRouter()

@router.get("/api/media/assets", response_model=c.PageResponse[c.MediaAssetCard])
def list_media_assets(
    request: Request,
    limit: int = 50,
    case_id: str | None = None,
    kind: str | None = None,
    annotation_status: str | None = None,
) -> c.PageResponse[c.MediaAssetCard]:
    return service.list_media_assets(request, limit, case_id, kind, annotation_status)


@router.get(
    "/api/media/assets/annotation-status",
    response_model=c.MediaAssetAnnotationStatusResponse,
)
def media_asset_annotation_status(
    request: Request,
    case_id: str | None = None,
    kind: str | None = None,
) -> c.MediaAssetAnnotationStatusResponse:
    return service.media_asset_annotation_status(request, case_id=case_id, kind=kind)


@router.get("/api/library/assets/{kind}/usage-ranking", response_model=c.MaterialUsageRankingReport)
def material_usage_ranking(
    request: Request,
    kind: c.SelectionMedium,
    case_id: str | None = None,
    top_n: int = 20,
) -> c.MaterialUsageRankingReport:
    return service.material_usage_ranking(request, kind, case_id, top_n)


@router.get(
    "/api/media/assets/clip-embeddings/status",
    response_model=c.ClipEmbeddingIndexStatusResponse,
)
def clip_embedding_status(
    request: Request,
    case_id: str,
    namespace: c.ClipEmbeddingNamespace = "all",
    asset_id: str | None = None,
) -> c.ClipEmbeddingIndexStatusResponse:
    return service.clip_embedding_status(request, case_id, namespace, asset_id)


@router.get(
    "/api/media/assets/clip-embeddings/jobs/{job_id}",
    response_model=c.ClipEmbeddingJobStatusResponse,
)
def clip_embedding_job_status(request: Request, job_id: str) -> c.ClipEmbeddingJobStatusResponse:
    return service.clip_embedding_job_status(request, job_id)


@router.post(
    "/api/media/assets/clip-embeddings/index",
    response_model=c.ClipEmbeddingIndexJobResponse,
    status_code=202,
)
def index_clip_embeddings(
    payload: c.ClipEmbeddingIndexRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> c.ClipEmbeddingIndexJobResponse:
    require_role(request, c.UserRole.operator)
    return service.index_clip_embeddings(payload, request, background_tasks)


@router.post("/api/media/assets", response_model=c.MediaAssetRecord, status_code=201)
def create_media_asset(payload: c.CreateMediaAssetFromUploadRequest, request: Request) -> c.MediaAssetRecord:
    require_role(request, c.UserRole.operator)
    return service.create_media_asset(payload, request)


@router.post("/api/media/assets/batch-stabilize", response_model=c.BatchMediaProcessResponse)
def batch_stabilize_assets(
    payload: c.BatchStabilizeMediaAssetsRequest, request: Request
) -> c.BatchMediaProcessResponse:
    require_role(request, c.UserRole.operator)
    return service.batch_stabilize_assets(payload, request)


@router.post("/api/media/assets/auto-match-replace", response_model=c.AutoMatchReplaceResponse)
def auto_match_replace(
    payload: c.AutoMatchReplaceRequest, request: Request
) -> c.AutoMatchReplaceResponse:
    require_role(request, c.UserRole.operator)
    return service.auto_match_replace(payload, request)


@router.post("/api/media/assets/{asset_id}/replace-source", response_model=c.MediaAssetReplaceResponse)
def replace_asset_source(
    asset_id: str, payload: c.MediaAssetReplaceSourceRequest, request: Request
) -> c.MediaAssetReplaceResponse:
    require_role(request, c.UserRole.operator)
    return service.replace_asset_source(asset_id, payload, request)


@router.get("/api/media/assets/{asset_id}", response_model=c.MediaAssetDetail)
def media_asset_detail(request: Request, asset_id: str) -> c.MediaAssetDetail:

    return service.media_asset_detail(request, asset_id)


@router.delete("/api/media/assets/{asset_id}", response_model=c.OkResponse)
def delete_media_asset(asset_id: str, request: Request) -> c.OkResponse:
    require_role(request, c.UserRole.operator)
    return service.delete_media_asset(request, asset_id)


@router.get("/api/media/assets/{asset_id}/preview-url", response_model=c.SignedUrlResponse)
def media_asset_preview(request: Request, asset_id: str) -> c.SignedUrlResponse:

    return service.media_asset_preview(request, asset_id)


@router.get("/api/media/assets/{asset_id}/content", response_class=FileResponse)
def media_asset_content(request: Request, asset_id: str) -> FileResponse:

    return service.media_asset_content(request, asset_id)


@router.post("/api/annotations/batch", response_model=c.AnnotationBatchResponse, status_code=202)
def batch_annotation(payload: c.AnnotationBatchRequest, request: Request) -> c.AnnotationBatchResponse:
    require_role(request, c.UserRole.operator)
    return service.batch_annotation(payload, request)


@router.get("/api/annotations/{asset_id}", response_model=c.AnnotationEditorVm)
def get_annotation(request: Request, asset_id: str) -> c.AnnotationEditorVm:

    return service.get_annotation(request, asset_id)


@router.patch("/api/annotations/{asset_id}", response_model=c.AnnotationEditorVm)
def patch_annotation(asset_id: str, payload: c.PatchAnnotationRequest, request: Request) -> c.AnnotationEditorVm:
    require_role(request, c.UserRole.operator)
    return service.patch_annotation(asset_id, payload, request)


@router.post("/api/annotations/{asset_id}/trim", response_model=c.TrimAnnotationResponse)
def trim_annotation(
    asset_id: str, payload: c.TrimAnnotationRequest, request: Request
) -> c.TrimAnnotationResponse:
    require_role(request, c.UserRole.operator)
    return service.trim_annotation(asset_id, payload, request)


@router.post("/api/annotations/{asset_id}/rerun", response_model=c.AnnotationRunResponse, status_code=202)
def rerun_annotation(
    asset_id: str, payload: c.RerunAnnotationRequest, request: Request
) -> c.AnnotationRunResponse:
    require_role(request, c.UserRole.operator)
    return service.rerun_annotation(asset_id, payload, request)
