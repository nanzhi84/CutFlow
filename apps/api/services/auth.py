from __future__ import annotations

from fastapi import Request, Response
from sqlalchemy import select

from apps.api.common import (
    auth,
    request_id,
)
from apps.api.dependencies import SESSION_COOKIE, current_user
from apps.api.services.auth_cookies import clear_session_cookie, set_session_cookie
from packages.core import contracts as c
from packages.core.auth import rate_limit
from packages.core.config import build_settings
from packages.core.storage.database import UserGenerationDefaultsRow
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError


def _client_identity(request: Request) -> str:
    """Best-effort client identity for rate-limit bucketing.

    Uses the direct peer address by default. ``X-Forwarded-For`` is client-supplied
    and is honored ONLY when ``auth.trust_forwarded_for`` is enabled (deployment
    behind a trusted proxy/LB that overwrites the header); otherwise trusting it
    would let an attacker rotate the header to mint a fresh limiter bucket per
    request and bypass the brute-force throttle. Falls back to a constant so the
    limiter still buckets when the peer is unknown (e.g. the TestClient)."""
    if build_settings().auth.trust_forwarded_for:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def register(request: Request, payload: c.RegisterRequest, response: Response) -> c.AuthResponse:
    # R2: throttle registration per client BEFORE doing any work, and count this
    # attempt toward the window.
    client_id = _client_identity(request)
    rate_limit.check_registration_rate_limit(client_id)
    rate_limit.record_registration_attempt(client_id)
    auth_response, token = auth(request).register(payload)
    set_session_cookie(response, request, token)
    return auth_response.model_copy(update={"request_id": request_id()})


def login(request: Request, payload: c.LoginRequest, response: Response) -> c.AuthResponse:

    identifier = (payload.identifier or payload.email or "").strip()
    # R2: reject if this client/identifier is already over the failed-login
    # threshold, then count failures and clear the bucket on success.
    client_id = _client_identity(request)
    rate_limit.check_login_rate_limit(client_id, identifier)
    try:
        auth_response, token = auth(request).login(identifier, payload.password)
    except NodeExecutionError:
        rate_limit.record_login_failure(client_id, identifier)
        raise
    rate_limit.record_login_success(client_id, identifier)
    set_session_cookie(response, request, token)
    return auth_response.model_copy(update={"request_id": request_id()})


def logout(request: Request, response: Response) -> c.OkResponse:
    current_user(request)
    auth(request).logout(request.cookies.get(SESSION_COOKIE))
    clear_session_cookie(response, request)
    return c.OkResponse(request_id=request_id())


def session(request: Request) -> c.SessionInfo:
    user = auth(request).authenticate_token(request.cookies.get(SESSION_COOKIE))
    return auth(request).session_info(user, request_id())


def me(request: Request) -> c.AuthUser:
    return auth(request).authenticate_token(request.cookies.get(SESSION_COOKIE))


def update_me(payload: c.UpdateMeRequest, request: Request) -> c.AuthUser:
    auth_service = auth(request)
    current_user = auth_service.authenticate_token(request.cookies.get(SESSION_COOKIE))
    user = auth_service.update_me(current_user.id, payload)
    if user is None:
        raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
    return user


def change_password(payload: c.ChangePasswordRequest, request: Request) -> c.OkResponse:
    token = request.cookies.get(SESSION_COOKIE)
    auth_service = auth(request)
    current_user = auth_service.authenticate_token(token)
    # R5: the DB service validates strength + revokes OTHER sessions, keeping
    # the caller's session identified by its raw cookie token.
    auth_service.change_password(current_user.id, payload, keep_token=token)
    return c.OkResponse(request_id=request_id())


def list_users(request: Request, limit: int = 50) -> c.PageResponse[c.AuthUser]:
    values = auth(request).list_users(limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_user(payload: c.AdminCreateUserRequest, request: Request) -> c.AuthUser:
    return auth(request).create_user(payload)


def patch_user(user_id: str, payload: c.AdminUpdateUserRequest, request: Request) -> c.AuthUser:
    user = auth(request).patch_user(user_id, payload)
    if user is None:
        raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
    return user


def registration_codes(request: Request, limit: int = 50) -> c.PageResponse[c.RegistrationCodePreview]:
    values = auth(request).list_registration_codes(limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_registration_code(
    payload: c.CreateRegistrationCodeRequest, request: Request
) -> c.CreatedRegistrationCode:
    return auth(request).create_registration_code(payload)


def patch_registration_code(
    code_id: str, payload: c.UpdateRegistrationCodeRequest, request: Request
) -> c.RegistrationCodePreview:
    code = auth(request).patch_registration_code(code_id, payload)
    if code is None:
        raise NodeExecutionError(c.ErrorCode.auth_registration_closed, "Registration code not found.")
    return code


def get_my_generation_defaults(request: Request) -> c.UserGenerationDefaults:
    """Return the caller's saved generation defaults.

    No saved record yet -> an all-``None`` ``UserGenerationDefaults`` (the caller
    falls back to the per-block system defaults). Backed by the SQL
    ``user_generation_defaults`` table."""
    user = auth(request).authenticate_token(request.cookies.get(SESSION_COOKIE))
    session_factory = request.app.state.sqlalchemy_session_factory
    with session_factory() as session:
        row = session.scalar(
            select(UserGenerationDefaultsRow).where(
                UserGenerationDefaultsRow.user_id == user.id
            )
        )
        if row is None:
            return c.UserGenerationDefaults()
        return c.UserGenerationDefaults.model_validate(row.settings)


def put_my_generation_defaults(
    request: Request, payload: c.UserGenerationDefaults
) -> c.UserGenerationDefaults:
    """Upsert the caller's generation defaults (full replace) and echo the saved value."""
    user = auth(request).authenticate_token(request.cookies.get(SESSION_COOKIE))
    settings_payload = payload.model_dump(mode="json")
    session_factory = request.app.state.sqlalchemy_session_factory
    with session_factory() as session:
        row = session.scalar(
            select(UserGenerationDefaultsRow).where(
                UserGenerationDefaultsRow.user_id == user.id
            )
        )
        if row is None:
            row = UserGenerationDefaultsRow(
                id=new_id("ugd"),
                user_id=user.id,
                preset_name="default",
                settings=settings_payload,
            )
            session.add(row)
        else:
            row.settings = settings_payload
            row.updated_at = c.utcnow()
        session.commit()
    return c.UserGenerationDefaults.model_validate(settings_payload)
