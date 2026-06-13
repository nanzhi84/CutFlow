"""MiniMax balance poller — MiniMax exposes NO balance API.

Pay-as-you-go balance is only visible in the platform.minimaxi.com console, so we
always report ``unsupported`` (even when a secret IS configured) rather than
fabricate a number. This plugin makes the "no API" decision explicit and tested,
instead of an implicit fall-through.
"""

from __future__ import annotations

from packages.core.contracts import ProviderBalanceItem

from ..base import BasePoller


class MiniMaxPoller(BasePoller):
    key = "minimax"
    prefixes = ("minimax",)

    #: MiniMax never needs a secret read — it is structurally unsupported.
    requires_secret = False

    def query(self, profile, *, secret, client, checked_at) -> ProviderBalanceItem:
        return self._unsupported(
            profile,
            checked_at,
            "MiniMax 按量付费余额无 API，仅 platform.minimaxi.com 后台可查",
        )
