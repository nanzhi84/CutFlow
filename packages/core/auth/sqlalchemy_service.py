from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.auth.service import ROLE_RANK, create_password_hasher
from packages.core.registration_codes import hash_registration_code
from packages.core.contracts import (
    AdminCreateUserRequest,
    AdminUpdateUserRequest,
    AuthResponse,
    AuthUser,
    ChangePasswordRequest,
    CreateRegistrationCodeRequest,
    ErrorCode,
    RegistrationCodePreview,
    RegisterRequest,
    SessionInfo,
    UpdateRegistrationCodeRequest,
    UpdateMeRequest,
    UserRole,
    utcnow,
)
from packages.core.storage.database import RegistrationCodeRow, SessionRow, UserRow
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError


def user_row_to_contract(row: UserRow) -> AuthUser:
    return AuthUser(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        role=UserRole(row.role),
        status=row.status,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def registration_code_row_to_contract(row: RegistrationCodeRow) -> RegistrationCodePreview:
    return RegistrationCodePreview(
        id=row.id,
        role=UserRole(row.role),
        status=row.status,
        max_uses=row.max_uses,
        used_count=row.used_count,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


@dataclass
class SqlAlchemyAuthService:
    session_factory: sessionmaker[Session]
    password_hasher: PasswordHasher
    session_ttl: timedelta = timedelta(days=7)

    def hash_password(self, password: str) -> str:
        return self.password_hasher.hash(password)

    def register(self, payload: RegisterRequest) -> tuple[AuthResponse, str]:
        registration_open = os.getenv("CUTAGENT_REGISTRATION_OPEN", "true").lower() == "true"
        with self.session_factory() as session:
            role = UserRole.viewer
            code = None
            if payload.registration_code:
                code = session.scalar(
                    select(RegistrationCodeRow).where(
                        RegistrationCodeRow.code_hash == hash_registration_code(payload.registration_code)
                    )
                )
                if code is None or code.status != "active":
                    raise NodeExecutionError(
                        ErrorCode.auth_registration_closed,
                        "Registration code is not active.",
                    )
                if code.expires_at and code.expires_at < utcnow():
                    raise NodeExecutionError(
                        ErrorCode.auth_registration_closed,
                        "Registration code is expired.",
                    )
                if code.max_uses is not None and code.used_count >= code.max_uses:
                    raise NodeExecutionError(
                        ErrorCode.auth_registration_closed,
                        "Registration code is exhausted.",
                    )
                role = UserRole(code.role)
            elif not registration_open:
                raise NodeExecutionError(ErrorCode.auth_registration_closed, "Registration is closed.")
            existing = session.scalar(select(UserRow).where(UserRow.email == payload.email))
            if existing is not None:
                raise NodeExecutionError(
                    ErrorCode.validation_invalid_options,
                    "Email is already registered.",
                )
            row = UserRow(
                id=new_id("usr"),
                email=payload.email,
                display_name=payload.display_name,
                password_hash=self.hash_password(payload.password),
                role=role.value,
                status="active",
            )
            session.add(row)
            if code is not None:
                code.used_count += 1
            session.flush()
            user = user_row_to_contract(row)
            auth_response, token = self._auth_response(session, user)
            session.commit()
            return auth_response, token

    def list_users(self, *, limit: int = 50) -> list[AuthUser]:
        with self.session_factory() as session:
            statement = select(UserRow).order_by(UserRow.created_at.asc()).limit(limit)
            return [user_row_to_contract(row) for row in session.scalars(statement)]

    def create_user(self, payload: AdminCreateUserRequest) -> AuthUser:
        with self.session_factory() as session:
            existing = session.scalar(select(UserRow).where(UserRow.email == payload.email))
            if existing is not None:
                raise NodeExecutionError(
                    ErrorCode.validation_invalid_options,
                    "Email is already registered.",
                )
            row = UserRow(
                id=new_id("usr"),
                email=payload.email,
                display_name=payload.display_name,
                password_hash=self.hash_password(payload.password or new_id("pwd")),
                role=payload.role.value,
                status="active",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return user_row_to_contract(row)

    def patch_user(self, user_id: str, payload: AdminUpdateUserRequest) -> AuthUser | None:
        with self.session_factory() as session:
            row = session.get(UserRow, user_id)
            if row is None:
                return None
            for key, value in payload.model_dump(exclude_none=True).items():
                if key == "role" and isinstance(value, UserRole):
                    value = value.value
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return user_row_to_contract(row)

    def update_me(self, user_id: str, payload: UpdateMeRequest) -> AuthUser | None:
        with self.session_factory() as session:
            row = session.get(UserRow, user_id)
            if row is None:
                return None
            if payload.display_name is not None:
                row.display_name = payload.display_name
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return user_row_to_contract(row)

    def change_password(self, user_id: str, payload: ChangePasswordRequest) -> None:
        with self.session_factory() as session:
            row = session.get(UserRow, user_id)
            if row is None or not self._verify_hash(row.password_hash, payload.old_password):
                raise NodeExecutionError(ErrorCode.auth_invalid_credentials, "Invalid credentials.")
            row.password_hash = self.hash_password(payload.new_password)
            row.updated_at = utcnow()
            session.commit()

    def list_registration_codes(self, *, limit: int = 50) -> list[RegistrationCodePreview]:
        with self.session_factory() as session:
            statement = select(RegistrationCodeRow).order_by(RegistrationCodeRow.created_at.desc()).limit(limit)
            return [registration_code_row_to_contract(row) for row in session.scalars(statement)]

    def create_registration_code(
        self, payload: CreateRegistrationCodeRequest
    ) -> RegistrationCodePreview:
        with self.session_factory() as session:
            row = RegistrationCodeRow(
                id=new_id("reg"),
                code_hash=hash_registration_code(new_id("reg_code")),
                role=payload.role.value,
                status="active",
                max_uses=payload.max_uses,
                used_count=0,
                expires_at=payload.expires_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return registration_code_row_to_contract(row)

    def patch_registration_code(
        self, code_id: str, payload: UpdateRegistrationCodeRequest
    ) -> RegistrationCodePreview | None:
        with self.session_factory() as session:
            row = session.get(RegistrationCodeRow, code_id)
            if row is None:
                return None
            for key, value in payload.model_dump(exclude_none=True).items():
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return registration_code_row_to_contract(row)

    def login(self, email: str, password: str) -> tuple[AuthResponse, str]:
        with self.session_factory() as session:
            row = session.scalar(select(UserRow).where(UserRow.email == email))
            if row is None or not self._verify_hash(row.password_hash, password):
                raise NodeExecutionError(ErrorCode.auth_invalid_credentials, "Invalid credentials.")
            if row.status == "disabled":
                raise NodeExecutionError(ErrorCode.auth_user_disabled, "User is disabled.")
            user = user_row_to_contract(row)
            auth_response, token = self._auth_response(session, user)
            session.commit()
            return auth_response, token

    def authenticate_token(self, token: str | None) -> AuthUser:
        if not token:
            raise NodeExecutionError(ErrorCode.auth_unauthorized, "Missing session.")
        with self.session_factory() as session:
            session_row = session.get(SessionRow, token)
            if session_row is None or session_row.revoked_at is not None:
                raise NodeExecutionError(ErrorCode.auth_unauthorized, "Invalid session.")
            if session_row.expires_at < utcnow():
                raise NodeExecutionError(ErrorCode.auth_unauthorized, "Session expired.")
            user_row = session.get(UserRow, session_row.user_id)
            if user_row is None or user_row.status == "disabled":
                raise NodeExecutionError(ErrorCode.auth_unauthorized, "User is not available.")
            return user_row_to_contract(user_row)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.session_factory() as session:
            session_row = session.get(SessionRow, token)
            if session_row is not None:
                session_row.revoked_at = utcnow()
                session.commit()

    def require_role(self, user: AuthUser, minimum: UserRole) -> None:
        if ROLE_RANK[user.role] < ROLE_RANK[minimum]:
            raise NodeExecutionError(ErrorCode.auth_forbidden, "Permission denied.")

    def session_info(self, user: AuthUser, request_id: str) -> SessionInfo:
        with self.session_factory() as session:
            session_row = session.scalar(
                select(SessionRow)
                .where(SessionRow.user_id == user.id)
                .where(SessionRow.revoked_at.is_(None))
                .order_by(SessionRow.expires_at.desc())
                .limit(1)
            )
            session_id = session_row.id if session_row else ""
        return SessionInfo(
            user=user,
            session_id=session_id,
            expires_at=utcnow() + self.session_ttl,
            request_id=request_id,
        )

    def _auth_response(self, session: Session, user: AuthUser) -> tuple[AuthResponse, str]:
        token = new_id("sess")
        expires_at = utcnow() + self.session_ttl
        session.add(SessionRow(id=token, user_id=user.id, expires_at=expires_at))
        request_id = "req_local"
        info = SessionInfo(user=user, session_id=token, expires_at=expires_at, request_id=request_id)
        return AuthResponse(user=user, session=info, request_id=request_id), token

    def _verify_hash(self, password_hash: str, password: str) -> bool:
        try:
            return self.password_hasher.verify(password_hash, password)
        except VerifyMismatchError:
            return False


def create_sqlalchemy_auth_service(session_factory: sessionmaker[Session]) -> SqlAlchemyAuthService:
    return SqlAlchemyAuthService(session_factory=session_factory, password_hasher=create_password_hasher())
