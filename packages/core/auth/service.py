from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError

from packages.core.contracts import (
    AuthResponse,
    AuthUser,
    ErrorCode,
    RegisterRequest,
    SessionInfo,
    UserRole,
    utcnow,
)
from packages.core.storage import Repository, get_repository
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.core.registration_codes import hash_registration_code


ROLE_RANK = {
    UserRole.viewer: 10,
    UserRole.operator: 20,
    UserRole.admin: 30,
}


def create_password_hasher() -> PasswordHasher:
    return PasswordHasher(type=Type.ID, time_cost=1, memory_cost=1024, parallelism=1)


@dataclass
class AuthService:
    repository: Repository
    password_hasher: PasswordHasher
    session_ttl: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if "usr_admin" in self.repository.users and "usr_admin" not in self.repository.password_hashes:
            self.repository.password_hashes["usr_admin"] = self.hash_password("local-admin")
        if "usr_viewer" in self.repository.users and "usr_viewer" not in self.repository.password_hashes:
            self.repository.password_hashes["usr_viewer"] = self.hash_password("local-viewer")

    def hash_password(self, password: str) -> str:
        return self.password_hasher.hash(password)

    def verify_password(self, user_id: str, password: str) -> bool:
        password_hash = self.repository.password_hashes.get(user_id)
        if not password_hash:
            return False
        try:
            return self.password_hasher.verify(password_hash, password)
        except VerifyMismatchError:
            return False

    def register(self, payload: RegisterRequest) -> tuple[AuthResponse, str]:
        registration_open = os.getenv("CUTAGENT_REGISTRATION_OPEN", "true").lower() == "true"
        role = UserRole.viewer
        code = None
        if payload.registration_code:
            code_id = self.repository.registration_code_hashes.get(hash_registration_code(payload.registration_code))
            code = self.repository.registration_codes.get(code_id or "")
            if code is None or code.status != "active":
                raise NodeExecutionError(ErrorCode.auth_registration_closed, "Registration code is not active.")
            if code.expires_at and code.expires_at < utcnow():
                raise NodeExecutionError(ErrorCode.auth_registration_closed, "Registration code is expired.")
            if code.max_uses is not None and code.used_count >= code.max_uses:
                raise NodeExecutionError(ErrorCode.auth_registration_closed, "Registration code is exhausted.")
            role = code.role
        elif not registration_open:
            raise NodeExecutionError(ErrorCode.auth_registration_closed, "Registration is closed.")
        if any(user.email == payload.email for user in self.repository.users.values()):
            raise NodeExecutionError(ErrorCode.validation_invalid_options, "Email is already registered.")
        user = AuthUser(
            id=new_id("usr"),
            email=payload.email,
            display_name=payload.display_name,
            role=role,
        )
        self.repository.users[user.id] = user
        self.repository.password_hashes[user.id] = self.hash_password(payload.password)
        if code is not None:
            self.repository.registration_codes[code.id] = code.model_copy(
                update={"used_count": code.used_count + 1, "updated_at": utcnow()}
            )
        return self._auth_response(user)

    def login(self, email: str, password: str) -> tuple[AuthResponse, str]:
        user = next((item for item in self.repository.users.values() if item.email == email), None)
        if user is None or not self.verify_password(user.id, password):
            raise NodeExecutionError(ErrorCode.auth_invalid_credentials, "Invalid credentials.")
        if user.status == "disabled":
            raise NodeExecutionError(ErrorCode.auth_user_disabled, "User is disabled.")
        return self._auth_response(user)

    def authenticate_token(self, token: str | None) -> AuthUser:
        if not token:
            raise NodeExecutionError(ErrorCode.auth_unauthorized, "Missing session.")
        session = self.repository.sessions.get(token)
        if session is None:
            raise NodeExecutionError(ErrorCode.auth_unauthorized, "Invalid session.")
        expires_at = session["expires_at"]
        if expires_at < utcnow():
            self.repository.sessions.pop(token, None)
            raise NodeExecutionError(ErrorCode.auth_unauthorized, "Session expired.")
        user = self.repository.users.get(session["user_id"])
        if user is None or user.status == "disabled":
            raise NodeExecutionError(ErrorCode.auth_unauthorized, "User is not available.")
        return user

    def logout(self, token: str | None) -> None:
        if token:
            self.repository.sessions.pop(token, None)

    def require_role(self, user: AuthUser, minimum: UserRole) -> None:
        if ROLE_RANK[user.role] < ROLE_RANK[minimum]:
            raise NodeExecutionError(ErrorCode.auth_forbidden, "Permission denied.")

    def session_info(self, user: AuthUser, request_id: str) -> SessionInfo:
        token = next(
            (
                session_id
                for session_id, session in self.repository.sessions.items()
                if session["user_id"] == user.id and session["expires_at"] >= utcnow()
            ),
            "",
        )
        return SessionInfo(
            user=user,
            session_id=token,
            expires_at=utcnow() + self.session_ttl,
            request_id=request_id,
        )

    def _auth_response(self, user: AuthUser) -> tuple[AuthResponse, str]:
        token = new_id("sess")
        expires_at = utcnow() + self.session_ttl
        self.repository.sessions[token] = {"user_id": user.id, "expires_at": expires_at}
        request_id = "req_local"
        session = SessionInfo(user=user, session_id=token, expires_at=expires_at, request_id=request_id)
        return AuthResponse(user=user, session=session, request_id=request_id), token


_AUTH_SERVICE = AuthService(get_repository(), create_password_hasher())


def get_auth_service() -> AuthService:
    return _AUTH_SERVICE
