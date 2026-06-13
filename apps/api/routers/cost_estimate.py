from __future__ import annotations

from fastapi import APIRouter, Request

from apps.api.dependencies import require_role
from apps.api.services import cost_estimate as service
from packages.core import contracts as c

router = APIRouter()


@router.post("/api/tts/estimate-cost", response_model=c.TtsCostEstimateResponse)
def estimate_tts_cost(payload: c.TtsCostEstimateRequest, request: Request) -> c.TtsCostEstimateResponse:
    require_role(request, c.UserRole.viewer)
    return service.estimate_tts_cost(payload, request)


@router.post("/api/video/estimate-cost", response_model=c.LipsyncCostEstimateResponse)
def estimate_lipsync_cost(
    payload: c.LipsyncCostEstimateRequest, request: Request
) -> c.LipsyncCostEstimateResponse:
    require_role(request, c.UserRole.viewer)
    return service.estimate_lipsync_cost(payload, request)
