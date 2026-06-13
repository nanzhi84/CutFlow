from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from apps.api.common import object_store, provider_repository, repository, request_id, secret_store
from apps.api.dependencies import require_role
from packages.ai.gateway import ProviderCall
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError
from packages.creative import reference_cookies
from packages.creative.reference_cookies import ReferenceCookieError
from packages.creative.reference_extract import ReferenceExtractError, extract_reference

router = APIRouter()


@router.post("/api/creative/reference-extract", response_model=c.ReferenceExtractResult)
async def reference_extract(payload: c.ReferenceExtractRequest, request: Request) -> c.ReferenceExtractResult:
    require_role(request, c.UserRole.operator)
    try:
        return await extract_reference(
            payload.url,
            payload.language,
            asr_invoke=lambda audio_url, language: _invoke_asr(request, audio_url, language),
            object_store=object_store(request),
            secret_store=secret_store(request),
        )
    except ReferenceExtractError as exc:
        raise NodeExecutionError(exc.code, exc.message, details=exc.details) from exc


@router.post(
    "/api/creative/reference-extractor/import-cookies",
    response_model=c.ReferenceCookieImportResponse,
)
def import_reference_cookies(
    payload: c.ReferenceCookieImportRequest, request: Request
) -> c.ReferenceCookieImportResponse:
    require_role(request, c.UserRole.operator)
    try:
        status = reference_cookies.import_cookies(
            secret_store(request),
            cookie_text=payload.cookie_text,
            cookie_format=payload.format,
            source=payload.source,
        )
    except ReferenceCookieError as exc:
        raise NodeExecutionError(exc.code, exc.message, details=exc.details) from exc
    return c.ReferenceCookieImportResponse(
        success=True,
        message=f"Imported {status.cookie_count} cookies.",
        status=status,
        request_id=request_id(),
    )


@router.post(
    "/api/creative/reference-extractor/test-cookies",
    response_model=c.ReferenceCookieTestResponse,
)
async def test_reference_cookies(
    payload: c.ReferenceCookieTestRequest, request: Request
) -> c.ReferenceCookieTestResponse:
    require_role(request, c.UserRole.operator)
    try:
        return await reference_cookies.test_cookies(
            secret_store(request),
            url=payload.url,
            request_id=request_id(),
        )
    except ReferenceCookieError as exc:
        raise NodeExecutionError(exc.code, exc.message, details=exc.details) from exc


@router.get(
    "/api/creative/reference-extractor/status",
    response_model=c.ReferenceExtractorStatusResponse,
)
def reference_extractor_status(request: Request) -> c.ReferenceExtractorStatusResponse:
    require_role(request, c.UserRole.viewer)
    refresh = reference_cookies.refresh_status()
    return c.ReferenceExtractorStatusResponse(
        cookie=reference_cookies.cookie_status(secret_store(request)),
        chrome_available=refresh["chrome_available"],
        chrome_path=refresh["chrome_path"],
        playwright_available=refresh["playwright_available"],
        auto_refresh_supported=refresh["auto_refresh_supported"],
        request_id=request_id(),
    )


@router.post("/api/creative/reference-extractor/refresh-cookies", status_code=410)
def refresh_reference_cookies(request: Request) -> JSONResponse:
    require_role(request, c.UserRole.operator)
    # Browser-profile auto-refresh (Playwright RPA) is intentionally NOT
    # supported in genesis. Operators import cookies manually instead.
    return JSONResponse(
        status_code=410,
        content={
            "error": {
                "code": "reference.refresh_not_supported",
                "message": (
                    "Automatic douyin cookie refresh is not supported. "
                    "Use POST /api/creative/reference-extractor/import-cookies "
                    "to paste cookies manually."
                ),
            },
            "request_id": request_id(),
        },
    )


def _invoke_asr(request: Request, audio_url: str, language: str) -> str:
    profile = _first_asr_profile(request)
    if profile is None:
        raise ReferenceExtractError(c.ErrorCode.reference_asr_failed, "ASR provider profile is not configured.")
    invocation, result = request.app.state.provider_gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="asr.transcribe",
            input={"audio_uri": audio_url, "language_hints": [language]},
        )
    )
    if result is None or invocation.error:
        details: dict[str, Any] = {"provider_invocation_id": invocation.id}
        if invocation.error is not None:
            code = invocation.error.code.value if hasattr(invocation.error.code, "value") else str(invocation.error.code)
            details["provider_error_code"] = code
        raise ReferenceExtractError(
            c.ErrorCode.reference_asr_failed,
            invocation.error.message if invocation.error else "ASR provider failed.",
            details=details,
        )
    text = result.output.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ReferenceExtractError(c.ErrorCode.reference_asr_failed, "ASR response did not include text.")
    return text.strip()


def _first_asr_profile(request: Request) -> c.ProviderProfile | None:
    db_repo = provider_repository(request)
    if db_repo is not None:
        profiles = db_repo.list_profiles(capability="asr.transcribe", limit=20)
    else:
        profiles = [profile for profile in repository(request).provider_profiles.values() if profile.capability == "asr.transcribe"]
    for profile in profiles:
        if profile.enabled:
            return profile
    return None
