"""OpenAI relay (new-api) balance poller.

Reads OpenAI-style billing under ``/v1/dashboard/billing/{subscription,usage}``.
This targets a new-api RELAY (e.g. neuromash), not api.openai.com — real OpenAI
returns HTML on these paths, which ``json_or_raise`` turns into ``error``. A
relay token with a very large ``hard_limit_usd`` (>= 1e6) means "no fixed quota
cap"; remaining is then meaningless, so we report used-only with ``unlimited`` in
detail instead of a misleading remaining number.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from packages.core.contracts import Money, ProviderBalanceItem

from ..base import BasePoller, Unauthorized, json_or_raise

_UNLIMITED_THRESHOLD = Decimal("1000000")


class OpenAIRelayPoller(BasePoller):
    key = "openai"
    prefixes = ("openai",)

    def query(self, profile, *, secret, client, checked_at) -> ProviderBalanceItem:
        root = self.base_url(profile, "https://api.openai.com")
        root = root[:-3].rstrip("/") if root.endswith("/v1") else root
        today = checked_at.date()
        params = {
            "start_date": (today - timedelta(days=99)).isoformat(),
            "end_date": (today + timedelta(days=1)).isoformat(),
        }
        headers = {"Authorization": f"Bearer {secret}"}
        try:
            subscription = json_or_raise(
                client.get(f"{root}/v1/dashboard/billing/subscription", headers=headers)
            )
            usage = json_or_raise(
                client.get(f"{root}/v1/dashboard/billing/usage", headers=headers, params=params)
            )
            hard_limit = subscription.get("hard_limit_usd")
            total_usage = usage.get("total_usage")
            if hard_limit is None or total_usage is None:
                return self._error(profile, checked_at, "响应缺少 hard_limit_usd/total_usage")
            hard = Decimal(str(hard_limit))
            used = (Decimal(str(total_usage)) / Decimal("100")).quantize(Decimal("0.01"))
            if hard >= _UNLIMITED_THRESHOLD:
                return self._ok(
                    profile,
                    checked_at,
                    balance=None,
                    detail=f"中转站令牌无固定额度上限；已用 ${used}",
                )
            remaining = (hard - used).quantize(Decimal("0.01"))
            return self._ok(
                profile,
                checked_at,
                balance=Money(amount=remaining, currency="USD"),
                detail="经 new-api 中转站；可能按 CNY 展示，以面板为准",
            )
        except Unauthorized:
            return self._unauthorized(profile, checked_at)
        except Exception as exc:  # noqa: BLE001
            return self._error(profile, checked_at, f"余额查询失败(可能 base_url 非中转站): {exc}", secret)
