"""DeepSeek balance poller (GET /user/balance, Bearer key)."""

from __future__ import annotations

from packages.core.contracts import ProviderBalanceItem

from ..base import BasePoller, Unauthorized, json_or_raise, money


class DeepSeekPoller(BasePoller):
    key = "deepseek"
    prefixes = ("deepseek",)

    def query(self, profile, *, secret, client, checked_at) -> ProviderBalanceItem:
        try:
            response = client.get(
                f"{self.base_url(profile, 'https://api.deepseek.com')}/user/balance",
                headers={"Authorization": f"Bearer {secret}"},
            )
            data = json_or_raise(response)
            infos = data.get("balance_infos") or []
            chosen = next((i for i in infos if i.get("currency") == "CNY"), infos[0] if infos else None)
            if not chosen:
                return self._error(profile, checked_at, "响应缺少 balance_infos")
            return self._ok(
                profile,
                checked_at,
                balance=money(chosen.get("total_balance"), chosen.get("currency")),
            )
        except Unauthorized:
            return self._unauthorized(profile, checked_at)
        except Exception as exc:  # noqa: BLE001 — pollers never raise
            return self._error(profile, checked_at, f"余额查询失败: {exc}", secret)
