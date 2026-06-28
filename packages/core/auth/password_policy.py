"""Server-side password strength policy (R5).

A single ``validate_password`` entry point enforced on BOTH registration and
password change. It raises :class:`NodeExecutionError` with
``ErrorCode.validation_invalid_options`` (400) so the API surfaces a clear
client error, matching the locked auth error-code lane (no new ErrorCode).

The rules enforce a minimum length, an upper length bound, a common-weak-password
blocklist, a character-variety floor, a category-count floor (3 of {lowercase,
uppercase, digit, symbol}), and a reuse guard that rejects passwords containing
the user's email/display-name tokens.
"""

from __future__ import annotations

import re

from packages.core.contracts import ErrorCode
from packages.core.workflow import NodeExecutionError

# Minimum length aligns with the contract floor (ChangePasswordRequest pins
# new_password to min_length=8) so the policy never contradicts the schema.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
# Distinct-character floor: rejects e.g. "aaaaaaaa" / "12121212".
MIN_DISTINCT_CHARS = 4
# Category floor: require at least N of {lower, upper, digit, symbol} for SHORT
# passwords. Long passphrases get a relaxed floor (see PASSPHRASE_* below) so
# high-entropy multi-word passphrases ("correct horse battery staple") are not
# rejected for lacking digits/symbols — length carries the entropy (NIST
# 800-63B favours length over forced composition).
MIN_CATEGORY_COUNT = 3
# Passwords at/above this length are treated as passphrases and only need
# PASSPHRASE_MIN_CATEGORY_COUNT categories.
PASSPHRASE_LENGTH = 16
PASSPHRASE_MIN_CATEGORY_COUNT = 2

# Lower-cased blocklist of obviously weak passwords (compared case-folded).
COMMON_WEAK_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "qwerty123",
        "admin123",
        "admin123456",
        "12345678",
        "123456789",
        "1234567890",
        "11111111",
        "iloveyou",
        "letmein123",
        "changeme123",
    }
)


def _category_count(password: str) -> int:
    """Count distinct character categories present in ``password``.

    Categories: lowercase letter, uppercase letter, digit, and "symbol"
    (anything that is not an ASCII alphanumeric — punctuation OR any non-ASCII
    character, so CJK / accented passwords count toward variety)."""
    categories = (
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"\d", password)),
        any((not ch.isascii()) or (not ch.isalnum()) for ch in password),
    )
    return sum(categories)


def validate_password(
    password: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
) -> None:
    """Validate ``password`` against the strength policy.

    Raises :class:`NodeExecutionError` (``validation_invalid_options`` / 400) on
    the first failing rule with a clear, user-facing message. Returns ``None``
    when the password is acceptable.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters.",
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            f"Password must be at most {PASSWORD_MAX_LENGTH} characters.",
        )
    folded = password.casefold()
    if folded in COMMON_WEAK_PASSWORDS:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "Password is too common; choose a stronger password.",
        )
    if len(set(password)) < MIN_DISTINCT_CHARS:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "Password has too little character variety.",
        )
    # Long passphrases earn a relaxed category floor; short passwords must mix
    # at least three character classes.
    required_categories = (
        PASSPHRASE_MIN_CATEGORY_COUNT
        if len(password) >= PASSPHRASE_LENGTH
        else MIN_CATEGORY_COUNT
    )
    if _category_count(password) < required_categories:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "Password is not complex enough; mix character types or use a "
            "longer passphrase.",
        )

    # Reuse guard: reject passwords that embed the user's identifying tokens.
    related: list[str] = []
    if display_name:
        related.append(display_name)
    if email:
        related.append(email)
        related.append(email.split("@", 1)[0])
    for value in related:
        normalized = value.strip().casefold()
        if len(normalized) >= 3 and normalized in folded:
            raise NodeExecutionError(
                ErrorCode.validation_invalid_options,
                "Password must not contain your email or display name.",
            )
