"""Auth domain: users, sessions, registration codes, and provider secrets."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import Field, model_validator

from .base import BaseListQuery, ContractModel, EntityMeta


class UserRole(str, Enum):
    admin = "admin"
    operator = "operator"
    viewer = "viewer"


class AuthUser(EntityMeta):
    email: str
    display_name: str
    role: UserRole = UserRole.viewer
    status: Literal["active", "disabled"] = "active"


class SessionInfo(ContractModel):
    user: AuthUser
    session_id: str
    expires_at: datetime
    request_id: str


class LoginRequest(ContractModel):
    identifier: str | None = None
    email: str | None = None
    password: str

    @model_validator(mode="after")
    def require_identifier(self) -> "LoginRequest":
        if not (self.identifier or self.email):
            raise ValueError("identifier or email is required")
        return self


class RegisterRequest(ContractModel):
    email: str
    password: str
    display_name: str
    registration_code: str | None = None


class AuthResponse(ContractModel):
    user: AuthUser
    session: SessionInfo
    request_id: str


class ChangePasswordRequest(ContractModel):
    old_password: str
    new_password: str = Field(min_length=8)


class UserListQuery(BaseListQuery):
    role: UserRole | None = None
    status: Literal["active", "disabled"] | None = None


class AdminCreateUserRequest(ContractModel):
    email: str
    display_name: str
    role: UserRole = UserRole.viewer
    password: str | None = None


class AdminUpdateUserRequest(ContractModel):
    display_name: str | None = None
    role: UserRole | None = None
    status: Literal["active", "disabled"] | None = None


class RegistrationCodeQuery(BaseListQuery):
    status: Literal["active", "disabled", "expired"] | None = None


class RegistrationCodePreview(ContractModel):
    id: str
    role: UserRole
    status: Literal["active", "disabled", "expired"]
    max_uses: int | None = None
    used_count: int
    purpose: str | None = None
    expires_at: datetime | None = None
    created_at: datetime


class CreatedRegistrationCode(RegistrationCodePreview):
    plaintext_code: str


class CreateRegistrationCodeRequest(ContractModel):
    role: UserRole
    custom_code: str | None = None
    purpose: str | None = None
    max_uses: int | None = None
    expires_at: datetime | None = None


class UpdateRegistrationCodeRequest(ContractModel):
    status: Literal["active", "disabled", "expired"] | None = None
    purpose: str | None = None
    expires_at: datetime | None = None


class UpdateMeRequest(ContractModel):
    display_name: str | None = None


class SecretQuery(BaseListQuery):
    provider_id: str | None = None
    environment: str | None = None
    status: str | None = None


class CreateSecretRequest(ContractModel):
    provider_id: str
    environment: Literal["local", "dev", "staging", "prod"]
    name: str
    plaintext_secret: str


class RotateSecretRequest(ContractModel):
    plaintext_secret: str
    reason: str


class DisableSecretRequest(ContractModel):
    reason: str


class SecretStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    rotated = "rotated"


class SecretRecord(EntityMeta):
    provider_id: str
    environment: Literal["local", "dev", "staging", "prod"]
    name: str
    secret_ref: str
    status: SecretStatus = SecretStatus.active
    rotated_from_secret_id: str | None = None
    rotated_at: datetime | None = None
    disabled_at: datetime | None = None


class SecretPreview(EntityMeta):
    provider_id: str
    environment: str
    name: str
    secret_ref: str | None = None
    status: SecretStatus = SecretStatus.active
    rotated_from_secret_id: str | None = None
    rotated_at: datetime | None = None
    disabled_at: datetime | None = None
    masked_value: str = "********"
