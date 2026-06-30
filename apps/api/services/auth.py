from __future__ import annotations


from fastapi import Request, Response
from sqlalchemy import select

from apps.api.common import (
    auth,
    page,
    repository,
    request_id,
)
from apps.api.dependencies import SESSION_COOKIE, current_user
from apps.api.services.auth_cookies import clear_session_cookie, set_session_cookie
from packages.core import contracts as c
from packages.core.auth import SqlAlchemyAuthService
from packages.core.auth import rate_limit
from packages.core.auth.password_policy import validate_password
from packages.core.config import build_settings
from packages.core.registration_codes import hash_registration_code
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
    current_user = auth(request).authenticate_token(request.cookies.get(SESSION_COOKIE))
    if isinstance(auth(request), SqlAlchemyAuthService):
        user = auth(request).update_me(current_user.id, payload)
        if user is None:
            raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
        return user
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return current_user
    return repository(request).patch(repository(request).users, current_user.id, updates)


def change_password(payload: c.ChangePasswordRequest, request: Request) -> c.OkResponse:
    token = request.cookies.get(SESSION_COOKIE)
    current_user = auth(request).authenticate_token(token)
    if isinstance(auth(request), SqlAlchemyAuthService):
        # R5: the DB service validates strength + revokes OTHER sessions, keeping
        # the caller's session identified by its raw cookie token.
        auth(request).change_password(current_user.id, payload, keep_token=token)
        return c.OkResponse(request_id=request_id())
    if not auth(request).verify_password(current_user.id, payload.old_password):
        raise NodeExecutionError(c.ErrorCode.auth_invalid_credentials, "Invalid credentials.")
    # R5: strength policy + revoke OTHER sessions on the in-memory backend too.
    validate_password(
        payload.new_password,
        email=current_user.email,
        display_name=current_user.display_name,
    )
    auth(request).repository.password_hashes[current_user.id] = auth(request).hash_password(payload.new_password)
    sessions = auth(request).repository.sessions
    for session_id in [
        sid
        for sid, session in sessions.items()
        if session["user_id"] == current_user.id and sid != token
    ]:
        sessions.pop(session_id, None)
    return c.OkResponse(request_id=request_id())


def list_users(request: Request, limit: int = 50) -> c.PageResponse[c.AuthUser]:
    if isinstance(auth(request), SqlAlchemyAuthService):
        values = auth(request).list_users(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository(request).users.values(), limit)


def create_user(payload: c.AdminCreateUserRequest, request: Request) -> c.AuthUser:
    if isinstance(auth(request), SqlAlchemyAuthService):
        return auth(request).create_user(payload)
    user = c.AuthUser(
        id=new_id("usr"),
        email=payload.email,
        display_name=payload.display_name,
        role=payload.role,
    )
    repository(request).users[user.id] = user
    repository(request).password_hashes[user.id] = auth(request).hash_password(payload.password or new_id("pwd"))
    return user


def patch_user(user_id: str, payload: c.AdminUpdateUserRequest, request: Request) -> c.AuthUser:
    if isinstance(auth(request), SqlAlchemyAuthService):
        user = auth(request).patch_user(user_id, payload)
        if user is None:
            raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
        return user
    users = repository(request).users
    if user_id not in users:
        raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
    updates = payload.model_dump(exclude_none=True)
    # R4: never let the last active admin be demoted or disabled (in-memory twin).
    _guard_last_admin_in_memory(users, user_id, updates)
    return repository(request).patch(users, user_id, updates)


def _guard_last_admin_in_memory(
    users: dict[str, c.AuthUser], user_id: str, updates: dict
) -> None:
    """Reject a patch that would leave zero active admins (in-memory R4 twin)."""
    row = users[user_id]
    if row.role != c.UserRole.admin or row.status != "active":
        return
    new_role = updates.get("role")
    demoting = "role" in updates and new_role != c.UserRole.admin
    disabling = updates.get("status") == "disabled"
    if not (demoting or disabling):
        return
    other_active_admins = sum(
        1
        for uid, user in users.items()
        if uid != user_id and user.role == c.UserRole.admin and user.status == "active"
    )
    if not other_active_admins:
        raise NodeExecutionError(
            c.ErrorCode.validation_conflict,
            "Cannot demote or disable the last active admin.",
        )


def registration_codes(request: Request, limit: int = 50) -> c.PageResponse[c.RegistrationCodePreview]:
    if isinstance(auth(request), SqlAlchemyAuthService):
        values = auth(request).list_registration_codes(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository(request).registration_codes.values(), limit)


def create_registration_code(
    payload: c.CreateRegistrationCodeRequest, request: Request
) -> c.CreatedRegistrationCode:
    if isinstance(auth(request), SqlAlchemyAuthService):
        return auth(request).create_registration_code(payload)
    plaintext_code = payload.custom_code.strip() if payload.custom_code else new_id("reg_code")
    if not plaintext_code:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Registration code cannot be empty.")
    code_hash = hash_registration_code(plaintext_code)
    if code_hash in repository(request).registration_code_hashes:
        raise NodeExecutionError(c.ErrorCode.validation_conflict, "Registration code already exists.")
    code = c.RegistrationCodePreview(
        id=new_id("reg"),
        role=payload.role,
        status="active",
        max_uses=payload.max_uses,
        used_count=0,
        purpose=payload.purpose,
        expires_at=payload.expires_at,
        created_at=c.utcnow(),
    )
    repository(request).registration_codes[code.id] = code
    repository(request).registration_code_hashes[code_hash] = code.id
    return c.CreatedRegistrationCode(**code.model_dump(), plaintext_code=plaintext_code)


def patch_registration_code(
    code_id: str, payload: c.UpdateRegistrationCodeRequest, request: Request
) -> c.RegistrationCodePreview:
    if isinstance(auth(request), SqlAlchemyAuthService):
        code = auth(request).patch_registration_code(code_id, payload)
        if code is None:
            raise NodeExecutionError(c.ErrorCode.auth_registration_closed, "Registration code not found.")
        return code
    return repository(request).patch(repository(request).registration_codes, code_id, payload.model_dump(exclude_none=True))


def get_my_generation_defaults(request: Request) -> c.UserGenerationDefaults:
    """Return the caller's saved generation defaults.

    No saved record yet -> an all-``None`` ``UserGenerationDefaults`` (the caller
    falls back to the per-block system defaults). Backed by the SQL
    ``user_generation_defaults`` table when running on the SQLAlchemy backend, and
    by the in-memory repository otherwise."""
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
