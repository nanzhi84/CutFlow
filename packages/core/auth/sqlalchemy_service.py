from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.auth.password_policy import validate_password
from packages.core.auth.service import ROLE_RANK, create_password_hasher
from packages.core.config import build_settings
from packages.core.registration_codes import hash_registration_code
from packages.core.contracts import (
    AdminCreateUserRequest,
    AdminUpdateUserRequest,
    AuthResponse,
    AuthUser,
    ChangePasswordRequest,
    CreatedRegistrationCode,
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


def hash_session_token(token: str) -> str:
    """Return the at-rest lookup key for a raw session token (R3).

    Session tokens are high-entropy random strings (``new_id("sess")``), so a
    plain SHA-256 hex digest (no salt) is sufficient: an attacker who reads the
    ``sessions`` table sees only the hash and cannot derive the raw cookie value
    needed to impersonate a user. The raw token is returned to the client once
    and never stored; every validation hashes the incoming token before lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
        purpose=row.purpose,
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
        # R5: enforce the server-side password strength policy on registration.
        validate_password(
            payload.password,
            email=payload.email,
            display_name=payload.display_name,
        )
        registration_open = build_settings().auth.registration_open
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
            updates = payload.model_dump(exclude_none=True)
            # R4: never let the last active admin be demoted or disabled.
            self._guard_last_admin(session, row, updates)
            for key, value in updates.items():
                if key == "role" and isinstance(value, UserRole):
                    value = value.value
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return user_row_to_contract(row)

    def _guard_last_admin(
        self, session: Session, row: UserRow, updates: dict[str, object]
    ) -> None:
        """Block a patch that would leave zero active admins (R4).

        Only relevant when ``row`` is itself currently an active admin and the
        patch would either change its role away from admin OR disable it. Counts
        OTHER active admins; if there are none, the change is rejected with
        ``ErrorCode.validation_conflict`` (409)."""
        if row.role != UserRole.admin.value or row.status != "active":
            return
        new_role = updates.get("role")
        if isinstance(new_role, UserRole):
            new_role = new_role.value
        demoting = "role" in updates and new_role != UserRole.admin.value
        disabling = updates.get("status") == "disabled"
        if not (demoting or disabling):
            return
        other_active_admins = session.scalar(
            select(func.count())
            .select_from(UserRow)
            .where(UserRow.role == UserRole.admin.value)
            .where(UserRow.status == "active")
            .where(UserRow.id != row.id)
        )
        if not other_active_admins:
            raise NodeExecutionError(
                ErrorCode.validation_conflict,
                "Cannot demote or disable the last active admin.",
            )

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

    def change_password(
        self,
        user_id: str,
        payload: ChangePasswordRequest,
        *,
        keep_token: str | None = None,
    ) -> None:
        with self.session_factory() as session:
            row = session.get(UserRow, user_id)
            if row is None or not self._verify_hash(row.password_hash, payload.old_password):
                raise NodeExecutionError(ErrorCode.auth_invalid_credentials, "Invalid credentials.")
            # R5: enforce password strength on the new password too.
            validate_password(
                payload.new_password,
                email=row.email,
                display_name=row.display_name,
            )
            row.password_hash = self.hash_password(payload.new_password)
            row.updated_at = utcnow()
            # R5: revoke all OTHER active sessions of this user so a leaked
            # session cannot survive a password change. Keep the caller's own
            # session if we can identify it from its raw token (hashed lookup);
            # otherwise revoke ALL sessions (the caller re-authenticates).
            keep_id = hash_session_token(keep_token) if keep_token else None
            now = utcnow()
            other_sessions = session.scalars(
                select(SessionRow)
                .where(SessionRow.user_id == user_id)
                .where(SessionRow.revoked_at.is_(None))
            )
            for session_row in other_sessions:
                if keep_id is not None and session_row.id == keep_id:
                    continue
                session_row.revoked_at = now
            session.commit()

    def list_registration_codes(self, *, limit: int = 50) -> list[RegistrationCodePreview]:
        with self.session_factory() as session:
            statement = select(RegistrationCodeRow).order_by(RegistrationCodeRow.created_at.desc()).limit(limit)
            return [registration_code_row_to_contract(row) for row in session.scalars(statement)]

    def create_registration_code(
        self, payload: CreateRegistrationCodeRequest
    ) -> CreatedRegistrationCode:
        plaintext_code = payload.custom_code.strip() if payload.custom_code else new_id("reg_code")
        if not plaintext_code:
            raise NodeExecutionError(ErrorCode.validation_invalid_options, "Registration code cannot be empty.")
        code_hash = hash_registration_code(plaintext_code)
        with self.session_factory() as session:
            existing = session.scalar(select(RegistrationCodeRow).where(RegistrationCodeRow.code_hash == code_hash))
            if existing is not None:
                raise NodeExecutionError(ErrorCode.validation_conflict, "Registration code already exists.")
            row = RegistrationCodeRow(
                id=new_id("reg"),
                code_hash=code_hash,
                role=payload.role.value,
                status="active",
                max_uses=payload.max_uses,
                used_count=0,
                purpose=payload.purpose,
                expires_at=payload.expires_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            preview = registration_code_row_to_contract(row)
            return CreatedRegistrationCode(**preview.model_dump(), plaintext_code=plaintext_code)

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

    def login(self, identifier: str, password: str) -> tuple[AuthResponse, str]:
        with self.session_factory() as session:
            row = session.scalar(
                select(UserRow).where(or_(UserRow.email == identifier, UserRow.display_name == identifier))
            )
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
            # R3: hash the incoming raw token before lookup (stored key is hashed).
            session_row = session.get(SessionRow, hash_session_token(token))
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
            # R3: hash before lookup — the stored key is the hashed token.
            session_row = session.get(SessionRow, hash_session_token(token))
            if session_row is not None:
                session_row.revoked_at = utcnow()
                session.commit()

    def require_role(self, user: AuthUser, minimum: UserRole) -> None:
        if ROLE_RANK[user.role] < ROLE_RANK[minimum]:
            raise NodeExecutionError(ErrorCode.auth_forbidden, "Permission denied.")

    def session_info(self, user: AuthUser, request_id: str) -> SessionInfo:
        # R3: SessionRow.id is now the HASH of the raw token, which cannot be
        # turned back into a usable session cookie value. The raw token is only
        # ever known to the client (held in its cookie), so the server returns an
        # empty session_id here — no caller/frontend depends on a non-empty value.
        return SessionInfo(
            user=user,
            session_id="",
            expires_at=utcnow() + self.session_ttl,
            request_id=request_id,
        )

    def _auth_response(self, session: Session, user: AuthUser) -> tuple[AuthResponse, str]:
        # R3: the RAW token goes to the client; only its hash is persisted as the
        # SessionRow PK (still a String column — no schema change).
        token = new_id("sess")
        expires_at = utcnow() + self.session_ttl
        session.add(
            SessionRow(id=hash_session_token(token), user_id=user.id, expires_at=expires_at)
        )
        request_id = "req_local"
        # session_id intentionally empty: the raw token cannot be recovered from
        # the stored hash, and no caller depends on a non-empty value here.
        info = SessionInfo(user=user, session_id="", expires_at=expires_at, request_id=request_id)
        return AuthResponse(user=user, session=info, request_id=request_id), token

    def _verify_hash(self, password_hash: str, password: str) -> bool:
        try:
            return self.password_hasher.verify(password_hash, password)
        except VerifyMismatchError:
            return False


def create_sqlalchemy_auth_service(session_factory: sessionmaker[Session]) -> SqlAlchemyAuthService:
    return SqlAlchemyAuthService(session_factory=session_factory, password_hasher=create_password_hasher())
