"""Account-group filtering + account matching + scheduled-at validation.

Faithful, side-effect-free port of the pure-logic helpers from the origin
``XiaoVmaoPublisherAdapter`` (``_filter_accounts_by_group`` / ``_group_match_tokens``
/ ``_match_account`` / ``_account_group_haystack``) and
``PublishService._normalize_scheduled_at`` / ``_normalize_publish_tags`` in
digital-human-Cutagent. These drive multi-account routing (which 小V猫 account
publishes for which Case/platform — §2.1 must-retain) and Asia/Shanghai
scheduling (§23.7), independent of any live CDP automation.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from packages.core.contracts import ErrorCode, PlatformAccount
from packages.core.workflow import NodeExecutionError

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# scheduled-at (Asia/Shanghai) validation
# ---------------------------------------------------------------------------


def normalize_scheduled_at(
    mode: str,
    value: datetime | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Normalize the publish ``scheduled_at`` for ``mode``.

    - ``immediate`` -> always ``None``.
    - ``scheduled`` -> require a value, interpret naive datetimes as Asia/Shanghai,
      convert tz-aware ones to Asia/Shanghai, and reject non-future times.

    Raises ``validation.invalid_options`` (the API-facing validation hard-fail).
    """
    if mode != "scheduled":
        return None
    if value is None:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "定时发布必须提供 scheduled_at。",
        )
    scheduled_at = value
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=SHANGHAI_TZ)
    else:
        scheduled_at = scheduled_at.astimezone(SHANGHAI_TZ)
    reference = (now.astimezone(SHANGHAI_TZ) if now else datetime.now(tz=SHANGHAI_TZ))
    if scheduled_at <= reference:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "定时时间必须晚于当前北京时间。",
        )
    return scheduled_at


# ---------------------------------------------------------------------------
# tags normalization (origin _normalize_publish_tags)
# ---------------------------------------------------------------------------


def normalize_publish_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags or []:
        for item in re.split(r"[\s,\n，、;；]+", str(raw_tag or "").strip()):
            cleaned = item.strip().lstrip("#").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


# ---------------------------------------------------------------------------
# account-group filtering + account matching (origin pure logic)
# ---------------------------------------------------------------------------


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "")).lower()


def _normalize_optional_text(value: str | None) -> str | None:
    normalized = _normalize_text(value)
    return normalized or None


def _account_group_haystack(account: PlatformAccount) -> str:
    return " ".join(
        part
        for part in [account.nickname, account.remark, account.sub_name, account.uid]
        if part
    )


def group_match_tokens(*, account_group: str | None = None, case_name: str | None = None) -> list[str]:
    preferred = _normalize_text(account_group)
    if preferred:
        return [preferred]
    tokens: list[str] = []
    for raw in [case_name]:
        normalized = _normalize_text(raw)
        if normalized and normalized not in tokens:
            tokens.append(normalized)
    return tokens


def filter_accounts_by_group(
    accounts: list[PlatformAccount],
    *,
    account_group: str | None = None,
    case_name: str | None = None,
) -> list[PlatformAccount]:
    tokens = group_match_tokens(account_group=account_group, case_name=case_name)
    if not tokens:
        return list(accounts)
    return [
        account
        for account in accounts
        if any(token in _normalize_text(_account_group_haystack(account)) for token in tokens)
    ]


def match_account(
    accounts: list[PlatformAccount],
    *,
    platform: str,
    account_group: str | None = None,
    case_name: str | None = None,
    account_uid: str | None = None,
    nickname_contains: str | None = None,
    sub_name_contains: str | None = None,
) -> PlatformAccount | None:
    """Select the publish account for ``platform``, mirroring the origin
    ``_match_account`` precedence: platform filter -> group filter -> explicit
    uid/nickname/sub_name match -> first remaining candidate."""
    candidates = [account for account in accounts if account.platform == platform]
    if not candidates:
        return None
    group_hint = _normalize_optional_text(account_group) or _normalize_optional_text(case_name)
    grouped = filter_accounts_by_group(candidates, account_group=account_group, case_name=case_name)
    if group_hint and grouped:
        candidates = grouped
    elif group_hint and not grouped:
        return None
    for account in candidates:
        if account_uid and account.uid == account_uid:
            return account
        if nickname_contains and nickname_contains in account.nickname:
            return account
        if sub_name_contains and sub_name_contains in account.sub_name:
            return account
    return candidates[0]
