"""HeyGem / RunningHub balance poller (POST /uc/openapi/accountStatus)."""

from __future__ import annotations

from packages.core.contracts import ProviderBalanceItem

from ..base import BasePoller, Unauthorized, json_or_raise, money


class HeyGemPoller(BasePoller):
    key = "heygem"
    prefixes = ("runninghub", "heygem")

    def query(self, profile, *, secret, client, checked_at) -> ProviderBalanceItem:
        try:
            response = client.post(
                f"{self.base_url(profile, 'https://www.runninghub.ai')}/uc/openapi/accountStatus",
                json={"apikey": secret},
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            )
            data = json_or_raise(response)
            if data.get("code") != 0:
                return self._error(profile, checked_at, f"接口返回 code={data.get('code')}")
            payload = data.get("data") or {}
            coins = payload.get("remainCoins")
            if coins is None:
                return self._error(profile, checked_at, "响应缺少 remainCoins")
            balance = money(payload.get("remainMoney"), "CNY") if payload.get("remainMoney") is not None else None
            return self._ok(
                profile,
                checked_at,
                balance=balance,
                quota_remaining=float(coins),
                unit="coins",
            )
        except Unauthorized:
            return self._unauthorized(profile, checked_at)
        except Exception as exc:  # noqa: BLE001
            return self._error(profile, checked_at, f"余额查询失败: {exc}", secret)
