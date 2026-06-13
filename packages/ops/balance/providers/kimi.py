"""Kimi (Moonshot) balance poller (GET /users/me/balance, Bearer key)."""

from __future__ import annotations

from packages.core.contracts import ProviderBalanceItem

from ..base import BasePoller, Unauthorized, json_or_raise, money


class KimiPoller(BasePoller):
    key = "kimi"
    prefixes = ("kimi", "moonshot")

    def query(self, profile, *, secret, client, checked_at) -> ProviderBalanceItem:
        try:
            response = client.get(
                f"{self.base_url(profile, 'https://api.moonshot.cn/v1')}/users/me/balance",
                headers={"Authorization": f"Bearer {secret}"},
            )
            data = json_or_raise(response)
            if data.get("code") != 0:
                return self._error(profile, checked_at, f"接口返回 code={data.get('code')}")
            payload = data.get("data") or {}
            balance = payload.get("available_balance")
            if balance is None:
                return self._error(profile, checked_at, "响应缺少 available_balance")
            return self._ok(profile, checked_at, balance=money(balance, "CNY"))
        except Unauthorized:
            return self._unauthorized(profile, checked_at)
        except Exception as exc:  # noqa: BLE001
            return self._error(profile, checked_at, f"余额查询失败: {exc}", secret)
