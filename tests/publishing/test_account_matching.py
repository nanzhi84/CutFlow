"""Scheduled-at validation + publish-tags normalization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.contracts import ErrorCode
from packages.core.workflow import NodeExecutionError
from packages.publishing.account_matching import (
    SHANGHAI_TZ,
    normalize_publish_tags,
    normalize_scheduled_at,
)


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
