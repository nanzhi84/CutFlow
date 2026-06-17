from __future__ import annotations

from fastapi import APIRouter, Request

from apps.api.common import page
from apps.api.dependencies import require_role
from apps.api.services import case_rubric as service
from packages.core import contracts as c

router = APIRouter()


@router.get("/api/cases/{case_id}/rubric", response_model=c.CaseRubric)
def get_rubric(request: Request, case_id: str) -> c.CaseRubric:
    return service.get_rubric(request, case_id)


@router.get("/api/cases/{case_id}/rubric/calibration", response_model=c.CalibrationReport)
def calibration(request: Request, case_id: str) -> c.CalibrationReport:
    return service.calibration(request, case_id)


@router.get(
    "/api/cases/{case_id}/rubric/bump-proposal",
    response_model=c.RubricBumpProposal | None,
)
def bump_proposal(request: Request, case_id: str) -> c.RubricBumpProposal | None:
    return service.bump_proposal(request, case_id)


@router.post(
    "/api/cases/{case_id}/rubric/bump-proposal/{proposal_id}/accept",
    response_model=c.CaseRubric,
)
def accept_bump(request: Request, case_id: str, proposal_id: str) -> c.CaseRubric:
    require_role(request, c.UserRole.operator)
    return service.accept_bump(request, case_id, proposal_id)


@router.post(
    "/api/cases/{case_id}/rubric/bump-proposal/{proposal_id}/reject",
    response_model=c.RubricBumpProposal,
)
def reject_bump(
    request: Request, case_id: str, proposal_id: str, payload: c.RejectBumpRequest
) -> c.RubricBumpProposal:
    require_role(request, c.UserRole.operator)
    return service.reject_bump(request, case_id, proposal_id, payload)


@router.get("/api/cases/{case_id}/predictions", response_model=c.PageResponse[c.ScorePrediction])
def predictions(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.ScorePrediction]:
    return page(service.list_predictions(request, case_id), limit)


@router.post(
    "/api/cases/{case_id}/finished-videos/{finished_video_id}/metrics",
    response_model=c.PerformanceObservation,
    status_code=202,
)
def backfill_metrics(
    request: Request,
    case_id: str,
    finished_video_id: str,
    payload: c.MetricsBackfillRequest,
) -> c.PerformanceObservation:
    require_role(request, c.UserRole.operator)
    return service.backfill_metrics(request, case_id, finished_video_id, payload)


@router.get("/api/cases/{case_id}/pending-retro", response_model=c.PendingRetroResponse)
def pending_retro(request: Request, case_id: str) -> c.PendingRetroResponse:
    return service.pending_retro(request, case_id)
