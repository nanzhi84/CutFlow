"""Aliyun BSS balance poller (DashScope / Qwen account-level balance).

This is the one provider that does NOT use httpx: Aliyun account balance is read
via the ``alibabacloud_bss_open_api`` SDK, which is NOT installed in the shared
venv. The import is therefore OPTIONAL — when the SDK is absent the poller
degrades to ``unsupported`` (never a crash). The adapter SHAPE is implemented so
that, once the SDK is added, the call wires up unchanged.

Note: Aliyun balance is account-level (a single total), not per-product, and the
secret here is an access-key id/secret PAIR rather than a single token. We accept
the secret as ``"<access_key_id>:<access_key_secret>"`` and degrade to
``unconfigured`` upstream when absent.
"""

from __future__ import annotations

import logging
from packages.core.contracts import ProviderBalanceItem

from ..base import BasePoller, money

logger = logging.getLogger(__name__)

_DASHBOARD_HINT = "RAM 用户需授予 AliyunBSSReadOnlyAccess；账户级总余额（非按产品拆分）"


def _split_credentials(secret: str) -> tuple[str, str] | None:
    if ":" not in secret:
        return None
    access_key_id, _, access_key_secret = secret.partition(":")
    if not access_key_id or not access_key_secret:
        return None
    return access_key_id, access_key_secret


class AliyunBssPoller(BasePoller):
    key = "aliyun"
    prefixes = ("aliyun", "dashscope", "qwen", "bailian")

    def query(self, profile, *, secret, client, checked_at) -> ProviderBalanceItem:
        try:
            from alibabacloud_bss_open_api.client import Client as BssClient  # type: ignore
            from alibabacloud_tea_openapi import models as open_api_models  # type: ignore
        except ImportError:
            return self._unsupported(
                profile,
                checked_at,
                "未安装 alibabacloud_bss_open_api 依赖（可选功能，需手动启用）",
            )

        creds = _split_credentials(secret or "")
        if creds is None:
            return self._error(
                profile,
                checked_at,
                "secret 需为 'access_key_id:access_key_secret' 形式",
            )
        access_key_id, access_key_secret = creds
        try:
            config = open_api_models.Config(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            )
            config.endpoint = "business.aliyuncs.com"
            response = BssClient(config).query_account_balance()
            data = response.body.data
            return self._ok(
                profile,
                checked_at,
                balance=money(data.available_cash_amount, getattr(data, "currency", "CNY")),
                detail=_DASHBOARD_HINT,
            )
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            low = text.lower()
            if any(t in text for t in ("NoPermission", "Forbidden")) or "not authorized" in low:
                return self._unauthorized(profile, checked_at, _DASHBOARD_HINT)
            return self._error(profile, checked_at, f"BSS 查询失败: {text}", secret)
