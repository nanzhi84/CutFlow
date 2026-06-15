"""Account-group filtering, account matching, scheduled-at validation, tags."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.contracts import ErrorCode, PlatformAccount
from packages.core.workflow import NodeExecutionError
from packages.publishing.account_matching import (
    SHANGHAI_TZ,
    filter_accounts_by_group,
    match_account,
    normalize_publish_tags,
    normalize_scheduled_at,
)


def _account(uid: str, platform: str, nickname: str = "", remark: str = "") -> PlatformAccount:
    return PlatformAccount(uid=uid, platform=platform, nickname=nickname, remark=remark, is_login=True)


def test_normalize_scheduled_at_immediate_is_none():
    assert normalize_scheduled_at("immediate", datetime.now() + timedelta(hours=1)) is None


def test_normalize_scheduled_at_requires_value_when_scheduled():
    with pytest.raises(NodeExecutionError) as exc:
        normalize_scheduled_at("scheduled", None)
    assert exc.value.error.code == ErrorCode.validation_invalid_options


def test_normalize_scheduled_at_rejects_past_time():
    with pytest.raises(NodeExecutionError) as exc:
        normalize_scheduled_at("scheduled", datetime(2000, 1, 1))
    assert exc.value.error.code == ErrorCode.validation_invalid_options


def test_normalize_scheduled_at_converts_to_shanghai():
    future_utc = datetime.now(timezone.utc) + timedelta(hours=5)
    result = normalize_scheduled_at("scheduled", future_utc)
    assert result is not None
    assert result.tzinfo == SHANGHAI_TZ


def test_normalize_publish_tags_splits_and_dedupes():
    assert normalize_publish_tags(["#补漆, 汽车", "汽车\n省钱"]) == ["补漆", "汽车", "省钱"]


def test_filter_accounts_by_group_matches_haystack():
    accounts = [
        _account("u1", "douyin", nickname="树影官方号"),
        _account("u2", "douyin", nickname="其他账号"),
    ]
    filtered = filter_accounts_by_group(accounts, account_group="树影")
    assert [a.uid for a in filtered] == ["u1"]


def test_match_account_prefers_group_then_uid():
    accounts = [
        _account("u1", "douyin", nickname="树影A"),
        _account("u2", "douyin", nickname="树影B"),
        _account("u3", "kuaishou", nickname="树影C"),
    ]
    matched = match_account(accounts, platform="douyin", account_group="树影", account_uid="u2")
    assert matched is not None and matched.uid == "u2"


def test_match_account_returns_none_when_group_has_no_platform_match():
    accounts = [_account("u1", "douyin", nickname="别的分组")]
    assert match_account(accounts, platform="douyin", account_group="树影") is None
