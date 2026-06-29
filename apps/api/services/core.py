from __future__ import annotations


from fastapi import Request
from fastapi.responses import JSONResponse

from apps.api.common import (
    repository,
    request_id,
)
from packages.core import contracts as c
from packages.core.config import validate_startup_settings
from packages.core.observability import metric_snapshot

def health(request: Request) -> c.OkResponse:

    return c.OkResponse(request_id=request_id())


def readiness(request: Request) -> JSONResponse:
    """Operational readiness probe (not part of the OpenAPI contract).

    In production this surfaces any unsafe-config preflight findings as a 503 so
    an orchestrator never routes traffic to a misconfigured replica. Outside
    production the preflight is a no-op, so this reflects a plain liveness-ish
    ready. PR4 (#67) extends this with live Redis/dependency checks.
    """
    settings = request.app.state.settings
    issues = validate_startup_settings(settings)
    payload = {
        "status": "ready" if not issues else "not_ready",
        "environment": settings.deployment.environment,
        "preflight_issues": issues,
        "request_id": request_id(),
    }
    return JSONResponse(status_code=200 if not issues else 503, content=payload)


def metrics(request: Request) -> str:

    return metric_snapshot(repository(request))
