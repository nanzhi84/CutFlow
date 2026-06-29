from __future__ import annotations


from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from apps.api.services import core as service
from packages.core import contracts as c

router = APIRouter()

@router.get("/api/health", response_model=c.OkResponse)
def health(request: Request) -> c.OkResponse:

    return service.health(request)


# Operational readiness probe; ``include_in_schema=False`` keeps it out of the
# OpenAPI contract (no schema.d.ts regen). Returns 503 in production when the
# startup preflight finds unsafe settings.
@router.get("/api/health/ready", include_in_schema=False)
def readiness(request: Request) -> JSONResponse:

    return service.readiness(request)


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:

    return service.metrics(request)
