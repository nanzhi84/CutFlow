from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from apps.api import common
from packages.core import contracts as c
from packages.core.observability import (
    bind_observability_context,
    record_api_request,
    reset_observability_context,
)
from packages.core.workflow import NodeExecutionError

SESSION_COOKIE = "cutagent_session"
PUBLIC_API_PATHS = {"/api/health", "/api/health/ready", "/api/health/network"}
PUBLIC_PATHS = {"/metrics"}
PUBLIC_API_PREFIXES = ("/api/auth/",)
IDEMPOTENT_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

request_id = common.request_id
access_logger = logging.getLogger("cutagent.api")


def current_user(request: Request) -> c.AuthUser:
    return common.auth(request).authenticate_token(request.cookies.get(SESSION_COOKIE))


def require_role(request: Request, minimum: c.UserRole) -> c.AuthUser:
    user = current_user(request)
    common.auth(request).require_role(user, minimum)
    return user


def node_error_response(exc: NodeExecutionError, *, status_override: int | None = None) -> JSONResponse:
    error = exc.error.model_copy(update={"request_id": request_id()})
    status = 400
    if error.code in {c.ErrorCode.auth_unauthorized, c.ErrorCode.auth_invalid_credentials}:
        status = 401
    elif error.code in {c.ErrorCode.auth_forbidden, c.ErrorCode.auth_user_disabled}:
        status = 403
    elif error.code in {c.ErrorCode.idempotency_conflict, c.ErrorCode.validation_conflict}:
        status = 409
    elif error.code in {c.ErrorCode.artifact_missing, c.ErrorCode.validation_missing_case}:
        status = 404
    elif error.code == c.ErrorCode.upload_too_large:
        status = 413
    elif error.code == c.ErrorCode.workflow_worker_lost:
        # The workflow runtime (Temporal) was unreachable or timed out on the
        # request path — an upstream-dependency failure, not a client error.
        status = 503
    return JSONResponse(
        status_code=status_override or status,
        content=c.ErrorEnvelope(error=error).model_dump(mode="json"),
        headers={"X-Request-Id": error.request_id or request_id()},
    )


def not_found_response(message: str) -> JSONResponse:
    return node_error_response(NodeExecutionError(c.ErrorCode.artifact_missing, message), status_override=404)


async def node_error_handler(request: Request, exc: NodeExecutionError) -> JSONResponse:
    return node_error_response(exc)


async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return node_error_response(
        NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            "Request validation failed.",
            details={"errors": jsonable_encoder(exc.errors())},
        ),
        status_override=422,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = c.ErrorCode.validation_invalid_options
    if exc.status_code == 401:
        code = c.ErrorCode.auth_unauthorized
    elif exc.status_code == 403:
        code = c.ErrorCode.auth_forbidden
    elif exc.status_code == 404:
        code = c.ErrorCode.artifact_missing
    elif exc.status_code == 409:
        code = c.ErrorCode.idempotency_conflict
    return node_error_response(NodeExecutionError(code, str(exc.detail)))


def requires_authenticated_api(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return False
    if path in PUBLIC_PATHS or path in PUBLIC_API_PATHS:
        return False
    return path.startswith("/api/") and not any(path.startswith(prefix) for prefix in PUBLIC_API_PREFIXES)


async def _read_response_body_capped(response, cap: int) -> tuple[bytes, bool]:
    """Buffer the response body but STOP as soon as it exceeds ``cap`` bytes.

    Returns ``(buffered, overflowed)``. On overflow it stops reading immediately —
    it does NOT drain the rest of ``response.body_iterator`` into memory — so the
    caller can stream ``buffered`` followed by the remaining chunks instead of
    buffering a potentially huge body just to cache it for idempotent replay. This
    is what keeps a large response from blowing the streaming memory boundary (#65).
    """
    buffered = b""
    async for chunk in response.body_iterator:
        buffered += chunk
        if len(buffered) > cap:
            return buffered, True
    return buffered, False


async def authenticate_api_request(request: Request, call_next):
    started_at = time.perf_counter()
    status_code = 500
    request_token = common.REQUEST_ID_CONTEXT.set(request.headers.get("X-Request-Id") or f"req_{uuid4().hex[:12]}")
    log_tokens = [
        bind_observability_context(
            request_id=request_id(),
            trace_id=request.headers.get("traceparent"),
        )
    ]
    request.state.request_id = request_id()
    user: c.AuthUser | None = None
    try:
        if requires_authenticated_api(request.url.path, request.method):
            try:
                user = current_user(request)
                log_tokens.append(bind_observability_context(user_id=user.id))
            except NodeExecutionError as exc:
                response = node_error_response(exc)
                status_code = response.status_code
                return response
        idempotency_key = request.headers.get("Idempotency-Key")
        if user is not None and idempotency_key and request.method in IDEMPOTENT_WRITE_METHODS:
            # Idempotency replay buffers the WHOLE request body (to hash it) and the
            # whole 2xx response body (to cache it). Reject large or streamed/binary
            # bodies so this control-plane-only feature can't blow up memory via the
            # middleware (#65). Real uploads must not carry an Idempotency-Key.
            api_settings = request.app.state.settings.api
            content_type = (request.headers.get("Content-Type") or "").lower()
            declared_length = request.headers.get("Content-Length")
            too_large = (
                declared_length is not None
                and declared_length.isdigit()
                and int(declared_length) > api_settings.idempotency_max_body_bytes
            )
            streamed_binary = content_type.startswith(
                ("multipart/form-data", "application/octet-stream")
            )
            if too_large or streamed_binary:
                response = node_error_response(
                    NodeExecutionError(
                        c.ErrorCode.upload_too_large,
                        "Idempotency-Key is only supported on small control-plane JSON "
                        "requests; this request body is too large or streamed.",
                    )
                )
                status_code = response.status_code
                return response
            body = await request.body()
            request_hash = hashlib.sha256(body).hexdigest()
            record_key = f"{user.id}:{idempotency_key}"
            record_method = request.method
            record_path = request.url.path
            store = common.idempotency_repository(request)
            existing = store.get(key=record_key, method=record_method, path=record_path, now=c.utcnow())
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    response = node_error_response(
                        NodeExecutionError(
                            c.ErrorCode.idempotency_conflict,
                            "Idempotency-Key was already used with a different request body.",
                        )
                    )
                    status_code = response.status_code
                    return response
                replay = JSONResponse(
                    # Spec 32.11: Idempotency-Key hit replays the original
                    # body with HTTP 200, regardless of the original status.
                    status_code=200,
                    content=existing["content"],
                    headers={"Idempotency-Replayed": "true"},
                )
                replay.headers["X-Request-Id"] = request_id()
                status_code = replay.status_code
                return replay

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            replayable_request = Request(request.scope, receive)
            response = await call_next(replayable_request)
            if 200 <= response.status_code < 300:
                response_body, response_overflow = await _read_response_body_capped(
                    response, api_settings.idempotency_max_response_bytes
                )
                if response_overflow:
                    # Too large to cache for replay. Stream it back WITHOUT ever
                    # buffering the whole body: emit the already-read prefix, then
                    # pass the remaining chunks straight through. The cap is a hard
                    # memory boundary — we never accumulate the full body (#65).
                    response.headers["X-Request-Id"] = request_id()
                    status_code = response.status_code
                    # Explicit degradation (never silent, per project rule): the
                    # response is too large to cache, so this Idempotency-Key will
                    # NOT replay — a client retry re-executes the write (#65).
                    access_logger.warning(
                        "idempotency response too large to cache; key will not replay",
                        extra={
                            "request_id": request_id(),
                            "idempotency_key": idempotency_key,
                            "path": record_path,
                            "cap_bytes": api_settings.idempotency_max_response_bytes,
                        },
                    )

                    async def _passthrough_body():
                        yield response_body
                        async for chunk in response.body_iterator:
                            yield chunk

                    return StreamingResponse(
                        _passthrough_body(),
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type=response.media_type,
                    )
                try:
                    content = json.loads(response_body) if response_body else None
                except json.JSONDecodeError:
                    response.headers["X-Request-Id"] = request_id()
                    passthrough = Response(
                        content=response_body,
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type=response.media_type,
                    )
                    status_code = passthrough.status_code
                    return passthrough
                expires_at = c.utcnow() + timedelta(hours=24)
                store.put(
                    key=record_key,
                    method=record_method,
                    path=record_path,
                    request_hash=request_hash,
                    response_status=response.status_code,
                    response_body=content,
                    expires_at=expires_at,
                )
                response.headers["X-Request-Id"] = request_id()
                stored = Response(
                    content=response_body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
                status_code = stored.status_code
                return stored
            response.headers["X-Request-Id"] = request_id()
            status_code = response.status_code
            return response
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id()
        status_code = response.status_code
        return response
    except Exception:
        access_logger.exception(
            "API request failed.",
            extra={
                "event": "api_error",
                "method": request.method,
                "route": request.url.path,
                "status": status_code,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            },
        )
        raise
    finally:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
        record_api_request(duration_ms / 1000, status_code)
        access_logger.info(
            "API request completed.",
            extra={
                "event": "api_request",
                "method": request.method,
                "route": request.url.path,
                "status": status_code,
                "duration_ms": duration_ms,
            },
        )
        for log_token in reversed(log_tokens):
            reset_observability_context(log_token)
        common.REQUEST_ID_CONTEXT.reset(request_token)
