"""Publishing-center account foundation service (clients / accounts / case targets).

Persistence goes through the SqlAlchemy accounts repo (the storage backend is
always SQL). Platform sessions are owned by 小V猫; this layer only persists binding
anchors and injects live login_state from 小V猫 when listing accounts.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from apps.api.common import accounts_repository, get_case, request_id, xiaovmao_login_manager
from apps.api.dependencies import not_found_response
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError

logger = logging.getLogger(__name__)


def _repo(request: Request):
    return accounts_repository(request)


# --- clients ---


def list_clients(
    request: Request, *, limit: int = 50, include_archived: bool = False
) -> c.PageResponse[c.Client]:
    items = _repo(request).list_clients(include_archived=include_archived, limit=limit)
    return c.PageResponse(items=items, total_hint=len(items), request_id=request_id())


def create_client(payload: c.CreateClientRequest, request: Request) -> c.Client:
    return _repo(request).create_client(name=payload.name, remark=payload.remark)


def patch_client(
    client_id: str, payload: c.PatchClientRequest, request: Request
) -> c.Client | JSONResponse:
    updated = _repo(request).patch_client(
        client_id, name=payload.name, remark=payload.remark, status=payload.status
    )
    if updated is None:
        return not_found_response("Client not found")
    return updated


def delete_client(client_id: str, request: Request) -> c.OkResponse | JSONResponse:
    repo = _repo(request)
    if repo.get_client(client_id) is None:
        return not_found_response("Client not found")
    repo.patch_client(client_id, status="archived")
    return c.OkResponse(request_id=request_id())


# --- accounts ---


def list_accounts(
    request: Request,
    *,
    client_id: str | None = None,
    platform: str | None = None,
    limit: int = 50,
    include_archived: bool = False,
) -> c.PageResponse[c.PublishAccount]:
    items = _repo(request).list_accounts(
        client_id=client_id, platform=platform, include_archived=include_archived, limit=limit
    )
    items = _inject_login_states(request, items)
    return c.PageResponse(items=items, total_hint=len(items), request_id=request_id())


def create_account(payload: c.CreatePublishAccountRequest, request: Request) -> c.PublishAccount:
    repo = _repo(request)
    if not repo.client_exists(payload.client_id):
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options, f"Client {payload.client_id} does not exist."
        )
    existing = repo.find_account_by_natural_key(
        client_id=payload.client_id, platform=payload.platform, account_name=payload.account_name
    )
    if existing is not None:
        raise NodeExecutionError(
            c.ErrorCode.validation_conflict,
            f"Account '{payload.account_name}' already exists for this client on {payload.platform}.",
        )
    return repo.create_account(
        client_id=payload.client_id,
        platform=payload.platform,
        account_name=payload.account_name,
        platform_uid=payload.platform_uid,
        xiaovmao_uid=payload.xiaovmao_uid,
    )


def patch_account(
    account_id: str, payload: c.PatchPublishAccountRequest, request: Request
) -> c.PublishAccount | JSONResponse:
    repo = _repo(request)
    current = repo.get_account(account_id)
    if current is None:
        return not_found_response("Publish account not found")
    # A rename must not collide with another account of the same client+platform.
    if payload.account_name is not None and payload.account_name != current.account_name:
        clash = repo.find_account_by_natural_key(
            client_id=current.client_id, platform=current.platform, account_name=payload.account_name
        )
        if clash is not None and clash.id != account_id:
            raise NodeExecutionError(
                c.ErrorCode.validation_conflict,
                f"Account '{payload.account_name}' already exists for this client on {current.platform}.",
            )
    if payload.status == "archived":
        _clear_account_publish_state(repo, request, account_id)
    updated = repo.patch_account(
        account_id,
        account_name=payload.account_name,
        platform_uid=payload.platform_uid,
        platform_uid_set="platform_uid" in payload.model_fields_set,
        xiaovmao_uid=payload.xiaovmao_uid,
        xiaovmao_uid_set="xiaovmao_uid" in payload.model_fields_set,
        status=payload.status,
    )
    if updated is None:
        return not_found_response("Publish account not found")
    return updated


def delete_account(account_id: str, request: Request) -> c.OkResponse | JSONResponse:
    repo = _repo(request)
    if repo.get_account(account_id) is None:
        return not_found_response("Publish account not found")
    _clear_account_publish_state(repo, request, account_id)
    repo.patch_account(account_id, status="archived")
    return c.OkResponse(request_id=request_id())


def _clear_account_publish_state(repo, request: Request, account_id: str) -> None:
    # Don't leave case targets bound to an archived account.
    repo.delete_targets_for_account(account_id)


def _probe_xiaovmao_accounts(request: Request) -> list[c.PlatformAccount] | None:
    try:
        accounts, available, _reason = xiaovmao_login_manager(request).probe_accounts()
    except Exception:
        logger.debug("xiaovmao account probe failed", exc_info=True)
        return None
    return accounts if available else None


def _inject_login_states(
    request: Request, accounts: list[c.PublishAccount]
) -> list[c.PublishAccount]:
    xiaovmao_accounts = _probe_xiaovmao_accounts(request)
    if xiaovmao_accounts is None:
        return [account.model_copy(update={"login_state": "unknown"}) for account in accounts]
    return [
        account.model_copy(
            update={"login_state": _login_state_for_account(account, xiaovmao_accounts)}
        )
        for account in accounts
    ]


def _login_state_for_account(
    account: c.PublishAccount, xiaovmao_accounts: list[c.PlatformAccount]
) -> c.PublishLoginState:
    match = _match_xiaovmao_account(account, xiaovmao_accounts)
    if match is None:
        return "logged_out"
    return "logged_in" if match.is_login else "logged_out"


def _match_xiaovmao_account(
    account: c.PublishAccount, xiaovmao_accounts: list[c.PlatformAccount]
) -> c.PlatformAccount | None:
    if account.xiaovmao_uid:
        for item in xiaovmao_accounts:
            if item.uid == account.xiaovmao_uid and item.platform == account.platform:
                return item
    for item in xiaovmao_accounts:
        if item.platform != account.platform:
            continue
        names = {item.nickname, item.remark, item.sub_name}
        if account.account_name in names:
            return item
    return None


# --- case targets ---


def list_case_targets(case_id: str, request: Request) -> c.PageResponse[c.CasePublishTarget]:
    items = _repo(request).list_targets(case_id)
    return c.PageResponse(items=items, total_hint=len(items), request_id=request_id())


def set_case_targets(
    case_id: str, payload: c.SetCasePublishTargetsRequest, request: Request
) -> c.PageResponse[c.CasePublishTarget]:
    get_case(request, case_id)  # raises validation_missing_case if the case doesn't exist
    repo = _repo(request)
    account_ids = list(dict.fromkeys(payload.account_ids))
    client_map = repo.accounts_client_map(account_ids)
    missing = [account_id for account_id in account_ids if account_id not in client_map]
    if missing:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options, f"Unknown publish account(s): {', '.join(missing)}."
        )
    if len(set(client_map.values())) > 1:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            "All publish targets for a case must belong to the same client.",
        )
    items = repo.set_targets(case_id, account_ids)
    return c.PageResponse(items=items, total_hint=len(items), request_id=request_id())
