from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, Response, UploadFile, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse

from apps.api.dependencies import require_role
from apps.api.services import jobs_runs as service
from packages.core import contracts as c

router = APIRouter()

@router.get("/api/cases/{case_id}/runs", response_model=c.PageResponse[c.RunCard])
def case_run_cards(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.RunCard]:
    return service.case_run_cards(request, case_id, limit)


@router.post("/api/jobs/digital-human-video", response_model=c.CreateJobResponse, status_code=201)
def create_digital_human_job(
    payload: c.CreateDigitalHumanVideoJobRequest, request: Request
) -> c.CreateJobResponse:
    require_role(request, c.UserRole.operator)
    return service.create_digital_human_job(payload, request)


@router.post(
    "/api/jobs/digital-human-video/estimate-cost",
    response_model=c.DigitalHumanVideoCostEstimateResponse,
)
def estimate_digital_human_video_cost(
    payload: c.CreateDigitalHumanVideoJobRequest, request: Request
) -> c.DigitalHumanVideoCostEstimateResponse:
    require_role(request, c.UserRole.operator)
    return service.estimate_digital_human_video_cost(payload, request)


@router.get("/api/jobs/{job_id}", response_model=c.JobDetailResponse)
def job_detail(request: Request, job_id: str) -> c.JobDetailResponse:

    return service.job_detail(request, job_id)


@router.post("/api/jobs/{job_id}/runs", response_model=c.WorkflowRunResponse, status_code=201)
def create_run(job_id: str, payload: c.CreateRunRequest, request: Request) -> c.WorkflowRunResponse:
    require_role(request, c.UserRole.operator)
    return service.create_run(job_id, payload, request)


@router.get("/api/runs/{run_id}", response_model=c.RunDetailResponse)
def run_detail(request: Request, run_id: str) -> c.RunDetailResponse:

    return service.run_detail(request, run_id)


@router.delete("/api/runs/{run_id}", response_model=c.OkResponse)
def delete_run_record(run_id: str, request: Request) -> c.OkResponse:
    require_role(request, c.UserRole.operator)
    return service.delete_run_record(run_id, request)


@router.post("/api/runs/{run_id}/cancel", response_model=c.RunActionResponse, status_code=202)
def cancel_run(run_id: str, payload: c.CancelRunRequest, request: Request) -> c.RunActionResponse:
    require_role(request, c.UserRole.operator)
    return service.cancel_run(run_id, payload, request)


@router.post("/api/runs/{run_id}/retry", response_model=c.RetryRunResponse, status_code=201)
def retry_run(run_id: str, payload: c.RetryRunRequest, request: Request) -> c.RetryRunResponse:
    require_role(request, c.UserRole.operator)
    return service.retry_run(run_id, payload, request)


@router.post("/api/runs/{run_id}/resume", response_model=c.ResumeRunResponse, status_code=201)
def resume_run(run_id: str, payload: c.ResumeRunRequest, request: Request) -> c.ResumeRunResponse:
    require_role(request, c.UserRole.operator)
    return service.resume_run(run_id, payload, request)


@router.get("/api/runs/{run_id}/report", response_model=c.RunReportResponse)
def run_report(request: Request, run_id: str) -> c.RunReportResponse:

    return service.run_report(request, run_id)


@router.get("/api/runs/{run_id}/artifacts", response_model=c.RunArtifactsResponse)
def run_artifacts(request: Request, run_id: str) -> c.RunArtifactsResponse:

    return service.run_artifacts(request, run_id)


@router.get("/api/runs/{run_id}/events", response_model=c.EventStreamTokenResponse)
def run_events(request: Request, run_id: str) -> c.EventStreamTokenResponse:

    return service.run_events(request, run_id)


@router.websocket("/ws/runs/{run_id}")
async def run_websocket(websocket: WebSocket, run_id: str) -> None:
    await service.run_websocket(websocket, run_id)
