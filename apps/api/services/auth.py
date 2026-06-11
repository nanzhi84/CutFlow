from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Request, Response, UploadFile
from fastapi.responses import JSONResponse

from apps.api.common import (
    auth,
    case_learning_repository,
    case_repository,
    ensure_artifact_ref,
    get_case,
    media_repository,
    object_store,
    ops_repository,
    page,
    production_repository,
    prompt_repository,
    provider_repository,
    publishing_repository,
    repository,
    request_id,
    secret_repository,
    secret_store,
    signed,
    upload_repository,
    workflow_runtime,
)
from apps.api.dependencies import SESSION_COOKIE, current_user, not_found_response
from packages.core import contracts as c
from packages.core.auth import SqlAlchemyAuthService
from packages.core.contracts.state_machines import assert_transition
from packages.core.observability import metric_snapshot
from packages.core.registration_codes import hash_registration_code
from packages.core.storage.object_store import parse_local_uri
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError

def register(request: Request, payload: c.RegisterRequest, response: Response) -> c.AuthResponse:

    auth_response, token = auth(request).register(payload)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return auth_response.model_copy(update={"request_id": request_id()})


def login(request: Request, payload: c.LoginRequest, response: Response) -> c.AuthResponse:

    identifier = (payload.identifier or payload.email or "").strip()
    auth_response, token = auth(request).login(identifier, payload.password)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return auth_response.model_copy(update={"request_id": request_id()})


def logout(request: Request, response: Response) -> c.OkResponse:
    current_user(request)
    auth(request).logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE)
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
    current_user = auth(request).authenticate_token(request.cookies.get(SESSION_COOKIE))
    if isinstance(auth(request), SqlAlchemyAuthService):
        auth(request).change_password(current_user.id, payload)
        return c.OkResponse(request_id=request_id())
    if not auth(request).verify_password(current_user.id, payload.old_password):
        raise NodeExecutionError(c.ErrorCode.auth_invalid_credentials, "Invalid credentials.")
    auth(request).repository.password_hashes[current_user.id] = auth(request).hash_password(payload.new_password)
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
    if user_id not in repository(request).users:
        raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
    updates = payload.model_dump(exclude_none=True)
    return repository(request).patch(repository(request).users, user_id, updates)


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
