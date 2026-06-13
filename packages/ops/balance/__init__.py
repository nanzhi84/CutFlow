"""Real, gated provider balance pollers.

A balance-poller PORT (:class:`BalancePoller`) + per-provider plugins (DeepSeek,
Kimi/Moonshot, OpenAI new-api relay, HeyGem/RunningHub, MiniMax-as-unsupported,
Aliyun BSS via optional SDK). The pollers are httpx adapters that DEGRADE
gracefully: a missing secret -> ``unconfigured``, no balance API -> ``unsupported``,
401/403 -> ``unauthorized``, transient failure -> ``error``. They never fabricate
a number and never raise.

Public entry points:
  - :func:`query_balance` — one profile -> :class:`ProviderBalanceItem`.
  - :func:`refresh_balances` — aggregate a list of profiles.
  - :class:`BalancePollerService` — opt-in periodic background refresh.
"""

from __future__ import annotations

from .port import BalancePoller
from .registry import build_pollers, query_balance
from .service import BalancePollerService, refresh_balances

__all__ = [
    "BalancePoller",
    "BalancePollerService",
    "build_pollers",
    "query_balance",
    "refresh_balances",
]
