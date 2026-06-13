"""The balance-poller PORT.

A :class:`BalancePoller` knows how to read ONE provider's remaining balance /
quota and map it into the shared :class:`ProviderBalanceItem` contract. Pollers
are pure adapters around an injected ``httpx.Client``: they NEVER raise, NEVER
open their own connections, and NEVER fabricate a number — a missing secret maps
to ``unconfigured``, a provider without a balance API maps to ``unsupported``,
and any transient/auth failure maps to ``error``/``unauthorized``.

The registry dispatches a :class:`ProviderProfile` to the poller whose
:meth:`handles` matches ``profile.provider_id``; the aggregating service fans the
profiles out over the registry. This keeps the port small and side-effect free
so it is trivially unit-testable with ``httpx.MockTransport``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

import httpx

from packages.core.contracts import ProviderBalanceItem, ProviderProfile


@runtime_checkable
class BalancePoller(Protocol):
    """One provider's balance adapter (a plugin)."""

    #: Human-facing key, e.g. ``"deepseek"``. Used for logging / diagnostics.
    key: str

    def handles(self, provider_id: str) -> bool:
        """Return True when this poller can read ``provider_id``'s balance."""
        ...

    def query(
        self,
        profile: ProviderProfile,
        *,
        secret: str | None,
        client: httpx.Client,
        checked_at: datetime,
    ) -> ProviderBalanceItem:
        """Read the balance for ``profile``; ``secret`` is None when unconfigured.

        MUST NOT raise. Failures are returned as an ``error``/``unauthorized``
        :class:`ProviderBalanceItem`, never as an exception.
        """
        ...
