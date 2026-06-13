"""Shared helpers for balance pollers.

Centralises the cross-provider concerns the original per-provider modules each
re-implemented: secret-scrubbing of error text, :class:`ProviderBalanceItem`
construction, ``Money`` coercion, base-url resolution from the profile options,
and a single ``httpx.Response`` -> JSON gate that turns 401/403 into a typed
:class:`Unauthorized` signal. ``BasePoller`` gives plugins a thin, DRY surface so
each provider file only contains the call shape + response parsing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from packages.core.contracts import Money, ProviderBalanceItem, ProviderProfile

logger = logging.getLogger(__name__)

# Mask credential-shaped tokens before any error text reaches a client. Full
# diagnostics stay in server logs only.
_SECRET_RE = re.compile(r"Bearer\s+\S+|sk-[A-Za-z0-9_\-]+|LTAI[A-Za-z0-9]+", re.IGNORECASE)


class Unauthorized(Exception):
    """Raised internally when a provider returns 401/403."""


def scrub(text: str, secret: str | None = None) -> str:
    """Strip credential-shaped substrings (and the live secret) from ``text``."""
    value = _SECRET_RE.sub("***", str(text))
    if secret:
        value = value.replace(secret, "***")
    return value[:240]


def money(value: object, currency: object = "CNY") -> Money:
    """Coerce a provider field into a ``Money`` (defaults to a 3-letter code)."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    code = str(currency or "CNY").upper()
    if len(code) != 3:
        code = "CNY"
    return Money(amount=amount, currency=code)


def json_or_raise(response: httpx.Response) -> dict:
    """Return the JSON body; raise :class:`Unauthorized` on 401/403 else 4xx/5xx.

    A non-JSON / non-object body (e.g. a real OpenAI HTML page behind a relay
    base-url) raises, which the caller maps to ``error``.
    """
    if response.status_code in {401, 403}:
        raise Unauthorized("unauthorized")
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


class BasePoller:
    """Mixin giving plugins DRY result construction + option lookups.

    Concrete pollers set ``key`` and the static ``provider_id`` prefixes they
    handle, then implement ``query``. The ``_ok``/``_error``/``_unauthorized``/
    ``_unsupported`` helpers all stamp ``checked_at`` and ``account_group`` from
    the profile so plugins stay focused on the provider's wire format.
    """

    key: str = ""
    #: provider_id prefixes this poller matches (lower-cased ``startswith``).
    prefixes: tuple[str, ...] = ()

    def handles(self, provider_id: str) -> bool:
        pid = provider_id.lower()
        return any(pid.startswith(prefix) for prefix in self.prefixes)

    # --- option helpers ---------------------------------------------------
    @staticmethod
    def base_url(profile: ProviderProfile, default: str) -> str:
        options = profile.default_options or {}
        value = options.get("base_url") if isinstance(options, Mapping) else None
        return str(value or default).rstrip("/")

    # --- result constructors ---------------------------------------------
    def _item(
        self,
        profile: ProviderProfile,
        *,
        status: str,
        checked_at: datetime,
        balance: Money | None = None,
        quota_remaining: float | None = None,
        unit: str | None = None,
        detail: str | None = None,
    ) -> ProviderBalanceItem:
        return ProviderBalanceItem(
            provider_id=profile.provider_id,
            account_group=profile.id,
            balance=balance,
            quota_remaining=quota_remaining,
            unit=unit,
            checked_at=checked_at,
            status=status,
            detail=detail,
        )

    def _ok(self, profile, checked_at, **kw) -> ProviderBalanceItem:
        return self._item(profile, status="ok", checked_at=checked_at, **kw)

    def _unsupported(self, profile, checked_at, detail: str) -> ProviderBalanceItem:
        return self._item(profile, status="unsupported", checked_at=checked_at, detail=detail)

    def _unauthorized(self, profile, checked_at, detail: str = "鉴权失败") -> ProviderBalanceItem:
        return self._item(profile, status="unauthorized", checked_at=checked_at, detail=detail)

    def _error(self, profile, checked_at, detail: str, secret: str | None = None) -> ProviderBalanceItem:
        logger.warning("balance poller %s error: %s", self.key, scrub(detail, secret))
        return self._item(profile, status="error", checked_at=checked_at, detail=scrub(detail, secret))
