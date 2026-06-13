"""Balance aggregation + the optional periodic background poller.

``refresh_balances`` fans a list of :class:`ProviderProfile` out over the
registry against ONE injected ``httpx.Client`` and returns the per-provider
:class:`ProviderBalanceItem` list. ``BalancePollerService`` wraps that in an
asyncio loop for an OPTIONAL periodic refresh — it is OFF by default
(``settings.balance.poller_enabled``) so no-key / test deployments never fan out
real provider calls. The loop owns no HTTP state of its own beyond a short-lived
per-tick client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime

import httpx

from packages.core.config import BalanceSettings, get_settings
from packages.core.contracts import ProviderBalanceItem, ProviderProfile, utcnow
from packages.core.storage.secret_store import SecretStore

from .port import BalancePoller
from .registry import build_pollers, query_balance

logger = logging.getLogger(__name__)


def refresh_balances(
    profiles: Iterable[ProviderProfile],
    *,
    secret_store: SecretStore,
    client: httpx.Client,
    pollers: list[BalancePoller] | None = None,
    checked_at: datetime | None = None,
) -> list[ProviderBalanceItem]:
    """Query every profile's balance against one shared client (no network when
    every profile is unconfigured/unsupported)."""
    checked = checked_at or utcnow()
    pollers = pollers if pollers is not None else build_pollers()
    return [
        query_balance(
            profile,
            secret_store=secret_store,
            client=client,
            pollers=pollers,
            checked_at=checked,
        )
        for profile in profiles
    ]


class BalancePollerService:
    """Opt-in periodic refresh of provider balances.

    Gated by ``settings.balance.poller_enabled`` (default OFF). ``on_results`` is
    an optional sink (e.g. persist snapshots / emit an invalidation signal); it is
    called with the freshly polled items after each tick.
    """

    def __init__(
        self,
        *,
        profiles_provider: Callable[[], Iterable[ProviderProfile]],
        secret_store: SecretStore,
        on_results: Callable[[list[ProviderBalanceItem]], None] | None = None,
        settings: BalanceSettings | None = None,
        pollers: list[BalancePoller] | None = None,
    ) -> None:
        self._profiles_provider = profiles_provider
        self._secret_store = secret_store
        self._on_results = on_results
        self._settings = settings or get_settings().balance
        self._pollers = pollers if pollers is not None else build_pollers()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self._settings.poller_enabled

    async def start(self) -> None:
        """Launch the periodic loop iff enabled; a no-op otherwise."""
        if not self._settings.poller_enabled:
            logger.info("balance poller disabled (settings.balance.poller_enabled=False)")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def refresh_once(self) -> list[ProviderBalanceItem]:
        """Synchronous single refresh (used by the loop and unit tests)."""
        with httpx.Client(trust_env=False, timeout=self._settings.request_timeout_seconds) as client:
            results = refresh_balances(
                self._profiles_provider(),
                secret_store=self._secret_store,
                client=client,
                pollers=self._pollers,
            )
        if self._on_results is not None:
            self._on_results(results)
        return results

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.refresh_once)
            except Exception:  # noqa: BLE001 — loop must survive a bad tick
                logger.exception("balance periodic refresh failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
