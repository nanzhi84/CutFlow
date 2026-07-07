from __future__ import annotations


from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from apps.api.dependencies import require_role
from apps.api.services import finished_videos as service
from packages.core import contracts as c

router = APIRouter()

@router.get("/api/videos/{video_version_id}/performance-attribution", response_model=c.PerformanceAttributionResponse)
def performance_attribution(request: Request, video_version_id: str) -> c.PerformanceAttributionResponse:

    return service.performance_attribution(request, video_version_id)


@router.get("/api/cases/{case_id}/finished-videos", response_model=c.PageResponse[c.FinishedVideo])
def case_finished_videos(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.FinishedVideo]:

    return service.case_finished_videos(request, case_id, limit)


@router.get(
    "/api/cases/{case_id}/finished-videos/downloads",
    response_model=c.BatchFinishedVideoDownloadResponse,
)
def case_finished_video_downloads(
    request: Request, case_id: str, ids: str | None = None
) -> c.BatchFinishedVideoDownloadResponse:
    return service.case_finished_video_downloads(request, case_id, ids)


@router.get("/api/finished-videos/{id}", response_model=c.FinishedVideoDetail)
def finished_video_detail(request: Request, id: str) -> c.FinishedVideoDetail:

    return service.finished_video_detail(request, id)


@router.get("/api/finished-videos/{id}/preview-url", response_model=c.SignedUrlResponse)
def finished_video_preview(request: Request, id: str) -> c.SignedUrlResponse:

    return service.finished_video_preview(request, id)


@router.get("/api/finished-videos/{id}/download", response_model=c.SignedUrlResponse)
def finished_video_download(request: Request, id: str) -> c.SignedUrlResponse:

    return service.finished_video_download(request, id)


@router.get(
    "/api/finished-videos/{id}/stream",
    response_model=None,
    response_class=FileResponse,
    responses={
        200: {
            "content": {
                "video/mp4": {"schema": {"type": "string", "format": "binary"}},
            },
            "description": "Finished video stream.",
        }
    },
)
def finished_video_stream(request: Request, id: str) -> Response:
    return service.finished_video_stream(request, id)


@router.get(
    "/api/finished-videos/{id}/jianying-draft/latest",
    response_model=c.LatestJianyingDraftPackageResponse,
)
def latest_jianying_draft(request: Request, id: str) -> c.LatestJianyingDraftPackageResponse:
    return service.latest_jianying_draft(id, request)


@router.get("/api/artifacts/{artifact_id}/download", response_model=None)
def artifact_download(
    request: Request, artifact_id: str, disposition: str | None = None
) -> Response:
    return service.artifact_download(request, artifact_id, disposition=disposition)


@router.delete("/api/finished-videos/{id}", response_model=c.OkResponse)
def delete_finished_video(id: str, request: Request, reason: str | None = None) -> c.OkResponse:
    require_role(request, c.UserRole.admin)
    return service.delete_finished_video(id, request, reason=reason)


@router.post(
    "/api/finished-videos/{id}/editor-handoff",
    response_model=c.EditorHandoffPackageArtifact,
    status_code=201,
)
def editor_handoff(
    id: str, payload: c.CreateEditorHandoffRequest, request: Request
) -> c.EditorHandoffPackageArtifact:
    require_role(request, c.UserRole.operator)
    return service.editor_handoff(id, payload, request)


@router.post(
    "/api/finished-videos/{id}/jianying-draft",
    response_model=c.JianyingDraftPackageArtifact,
    status_code=201,
)
def jianying_draft(
    id: str, payload: c.CreateJianyingDraftRequest, request: Request
) -> c.JianyingDraftPackageArtifact:
    require_role(request, c.UserRole.operator)
    return service.jianying_draft(id, payload, request)
