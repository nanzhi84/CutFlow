from __future__ import annotations

import pytest

from packages.core.auth.password_policy import validate_password
from packages.core.contracts import ErrorCode
from packages.core.workflow import NodeExecutionError


def _reject_code(password: str, **kwargs) -> ErrorCode:
    with pytest.raises(NodeExecutionError) as exc:
        validate_password(password, **kwargs)
    return exc.value.error.code


def test_strong_short_password_with_three_categories_is_accepted() -> None:
    # 12 chars, lower+upper+digit+symbol — comfortably strong.
    validate_password("Str0ng-Pass!")


def test_long_passphrase_is_accepted_even_with_two_categories() -> None:
    # No digits/uppercase, but >= 16 chars: length carries the entropy.
    validate_password("correct horse battery staple")


def test_too_short_password_is_rejected() -> None:
    assert _reject_code("Ab1!xy") == ErrorCode.validation_invalid_options


def test_too_long_password_is_rejected() -> None:
    assert _reject_code("Aa1!" + "x" * 200) == ErrorCode.validation_invalid_options


def test_common_weak_password_is_rejected() -> None:
    assert _reject_code("password123") == ErrorCode.validation_invalid_options


def test_low_variety_password_is_rejected() -> None:
    assert _reject_code("aaaaaaaa") == ErrorCode.validation_invalid_options


def test_short_password_needs_three_categories() -> None:
    # 10 chars, only lower+digit (two categories) -> rejected.
    assert _reject_code("abcdef1234") == ErrorCode.validation_invalid_options


def test_password_containing_email_local_part_is_rejected() -> None:
    code = _reject_code("Alice-Secret-99", email="alice@example.com")
    assert code == ErrorCode.validation_invalid_options


def test_password_containing_display_name_is_rejected() -> None:
    code = _reject_code("Wonderland-2026!", display_name="wonderland")
    assert code == ErrorCode.validation_invalid_options


def test_reuse_guard_ignores_short_tokens() -> None:
    # A 2-char display name must not block every password (>=3 char rule).
    validate_password("Str0ng-Pass!", display_name="ab", email="ab@x.io")
