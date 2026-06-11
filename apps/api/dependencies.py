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
from fastapi.responses import JSONResponse

from apps.api import common
from packages.core import contracts as c
from packages.core.observability import (
    bind_observability_context,
    record_api_request,
    reset_observability_context,
)
from packages.core.workflow import NodeExecutionError

SESSION_COOKIE = "cutagent_session"
PUBLIC_API_PATHS = {"/api/health"}
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
    elif error.code == c.ErrorCode.idempotency_conflict:
        status = 409
    elif error.code in {c.ErrorCode.artifact_missing, c.ErrorCode.validation_missing_case}:
        status = 404
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
            body = await request.body()
            request_hash = hashlib.sha256(body).hexdigest()
            record_key = f"{user.id}:{idempotency_key}"
            record_method = request.method
            record_path = request.url.path
            store = common.idempotency_repository(request)
            existing = (
                store.get(key=record_key, method=record_method, path=record_path, now=c.utcnow())
                if store is not None
                else common.repository(request).idempotency_records.get(f"{record_key}:{record_method}:{record_path}")
            )
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
                response_body = b""
                async for chunk in response.body_iterator:
                    response_body += chunk
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
                if store is not None:
                    store.put(
                        key=record_key,
                        method=record_method,
                        path=record_path,
                        request_hash=request_hash,
                        response_status=response.status_code,
                        response_body=content,
                        expires_at=expires_at,
                    )
                else:
                    common.repository(request).idempotency_records[f"{record_key}:{record_method}:{record_path}"] = {
                        "request_hash": request_hash,
                        "content": content,
                        "status_code": response.status_code,
                        "expires_at": expires_at,
                    }
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
