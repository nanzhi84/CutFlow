"""Compatibility shim for the balance pollers.

The real, gated balance-poller architecture lives in :mod:`packages.ops.balance`
(a PORT + per-provider plugins). This module preserves the historical
``query_provider_balance`` entry point — used by the API providers service — by
delegating to the ops subpackage so there is a single source of truth.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from packages.core.contracts import ProviderBalanceItem, ProviderProfile
from packages.core.storage.secret_store import SecretStore
from packages.ops.balance import query_balance

__all__ = ["query_provider_balance"]


def query_provider_balance(
    profile: ProviderProfile,
    *,
    secret_store: SecretStore,
    http_client: httpx.Client,
    checked_at: datetime | None = None,
) -> ProviderBalanceItem:
    """Read one provider's balance (delegates to :mod:`packages.ops.balance`)."""
    return query_balance(
        profile,
        secret_store=secret_store,
        client=http_client,
        checked_at=checked_at,
    )
