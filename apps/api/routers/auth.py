from __future__ import annotations


from fastapi import APIRouter, Request, Response

from apps.api.dependencies import require_role
from apps.api.services import auth as service
from packages.core import contracts as c

router = APIRouter()

@router.post("/api/auth/register", response_model=c.AuthResponse, status_code=201)
def register(request: Request, payload: c.RegisterRequest, response: Response) -> c.AuthResponse:

    return service.register(request, payload, response)


@router.post("/api/auth/login", response_model=c.AuthResponse)
def login(request: Request, payload: c.LoginRequest, response: Response) -> c.AuthResponse:

    return service.login(request, payload, response)


@router.post("/api/auth/logout", response_model=c.OkResponse)
def logout(request: Request, response: Response) -> c.OkResponse:
    return service.logout(request, response)


@router.get("/api/auth/session", response_model=c.SessionInfo)
def session(request: Request) -> c.SessionInfo:
    return service.session(request)


@router.get("/api/auth/me", response_model=c.AuthUser)
def me(request: Request) -> c.AuthUser:
    return service.me(request)


@router.patch("/api/auth/me", response_model=c.AuthUser)
def update_me(payload: c.UpdateMeRequest, request: Request) -> c.AuthUser:
    return service.update_me(payload, request)


@router.post("/api/auth/me/change-password", response_model=c.OkResponse)
def change_password(payload: c.ChangePasswordRequest, request: Request) -> c.OkResponse:
    return service.change_password(payload, request)


@router.get("/api/auth/me/generation-defaults", response_model=c.UserGenerationDefaults)
def get_generation_defaults(request: Request) -> c.UserGenerationDefaults:
    require_role(request, c.UserRole.viewer)
    return service.get_my_generation_defaults(request)


@router.put("/api/auth/me/generation-defaults", response_model=c.UserGenerationDefaults)
def put_generation_defaults(
    payload: c.UserGenerationDefaults, request: Request
) -> c.UserGenerationDefaults:
    require_role(request, c.UserRole.viewer)
    return service.put_my_generation_defaults(request, payload)


@router.get("/api/auth/users", response_model=c.PageResponse[c.AuthUser])
def list_users(request: Request, limit: int = 50) -> c.PageResponse[c.AuthUser]:
    require_role(request, c.UserRole.admin)
    return service.list_users(request, limit)


@router.post("/api/auth/users", response_model=c.AuthUser, status_code=201)
def create_user(payload: c.AdminCreateUserRequest, request: Request) -> c.AuthUser:
    require_role(request, c.UserRole.admin)
    return service.create_user(payload, request)


@router.patch("/api/auth/users/{user_id}", response_model=c.AuthUser)
def patch_user(user_id: str, payload: c.AdminUpdateUserRequest, request: Request) -> c.AuthUser:
    require_role(request, c.UserRole.admin)
    return service.patch_user(user_id, payload, request)


@router.get("/api/auth/registration-codes", response_model=c.PageResponse[c.RegistrationCodePreview])
def registration_codes(request: Request, limit: int = 50) -> c.PageResponse[c.RegistrationCodePreview]:
    require_role(request, c.UserRole.admin)
    return service.registration_codes(request, limit)


@router.post(
    "/api/auth/registration-codes",
    response_model=c.CreatedRegistrationCode,
    status_code=201,
)
def create_registration_code(
    payload: c.CreateRegistrationCodeRequest, request: Request
) -> c.CreatedRegistrationCode:
    require_role(request, c.UserRole.admin)
    return service.create_registration_code(payload, request)


@router.patch(
    "/api/auth/registration-codes/{code_id}",
    response_model=c.RegistrationCodePreview,
)
def patch_registration_code(
    code_id: str, payload: c.UpdateRegistrationCodeRequest, request: Request
) -> c.RegistrationCodePreview:
    require_role(request, c.UserRole.admin)
    return service.patch_registration_code(code_id, payload, request)
