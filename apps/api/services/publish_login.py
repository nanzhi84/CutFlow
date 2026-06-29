"""CDP-driven 小V猫 login orchestration for publish accounts.

The dashboard keeps the QR-login UX, but the underlying session source of truth is
小V猫. QR frames are streamed over WebSocket; successful completion binds the local
account anchor to the 小V猫 account uid. No platform session is stored in DB or
SecretStore.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from apps.api.common import accounts_repository, request_id, xiaovmao_login_manager
from apps.api.dependencies import not_found_response
from apps.api.services.publish_accounts import (
    _login_state_for_account,
    _probe_xiaovmao_accounts,
)
from packages.core import contracts as c
from packages.core.contracts.base import utcnow
from packages.core.storage.repository import new_id


def _repo(request: Request):
    return accounts_repository(request)


def _is_active_account(account: c.PublishAccount | None) -> bool:
    return account is not None and account.status == "active"


def begin_login(account_id: str, request: Request) -> c.BeginLoginResponse | JSONResponse:
    repo = _repo(request)
    account = repo.get_account(account_id)
    if not _is_active_account(account):
        return not_found_response("Publish account not found")

    login_id = new_id("login")

    def bind_xiaovmao_account(platform_account: c.PlatformAccount) -> c.PublishAccount | None:
        current = repo.get_account(account_id)
        if not _is_active_account(current):
            return None
        updated = repo.patch_account(
            account_id,
            xiaovmao_uid=platform_account.uid,
            xiaovmao_uid_set=True,
        )
        if updated is None:
            return None
        return updated.model_copy(update={"login_state": "logged_in"})

    xiaovmao_login_manager(request).begin(login_id, account, on_account=bind_xiaovmao_account)
    return c.BeginLoginResponse(
        login_id=login_id,
        account_id=account_id,
        platform=account.platform,
        status="pending",
        stream_path=f"/api/publish/accounts/login/{login_id}/stream",
        request_id=request_id(),
    )


def poll_login(
    account_id: str, login_id: str, request: Request
) -> c.LoginStatusResponse | JSONResponse:
    manager = xiaovmao_login_manager(request)
    session = manager.poll(login_id)
    if session is None or session.account_id != account_id:
        return not_found_response("Login session not found")
    account = _repo(request).get_account(account_id)
    if not _is_active_account(account):
        manager.cancel(login_id)
        return not_found_response("Login session not found")
    return c.LoginStatusResponse(
        login_id=login_id,
        account_id=account_id,
        status=session.status,
        detail=session.detail,
        login_state=session.login_state,
        request_id=request_id(),
    )


def cancel_login(account_id: str, login_id: str, request: Request) -> c.OkResponse | JSONResponse:
    manager = xiaovmao_login_manager(request)
    session = manager.poll(login_id)
    if session is None or session.account_id != account_id:
        return not_found_response("Login session not found")
    manager.cancel(login_id)
    return c.OkResponse(request_id=request_id())


def validate_session(account_id: str, request: Request) -> c.ValidateSessionResponse | JSONResponse:
    repo = _repo(request)
    account = repo.get_account(account_id)
    if not _is_active_account(account):
        return not_found_response("Publish account not found")
    xiaovmao_accounts = _probe_xiaovmao_accounts(request)
    login_state = (
        "unknown" if xiaovmao_accounts is None else _login_state_for_account(account, xiaovmao_accounts)
    )
    return c.ValidateSessionResponse(
        account_id=account_id,
        login_state=login_state,
        last_checked_at=utcnow(),
        request_id=request_id(),
    )
