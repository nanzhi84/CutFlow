"""Volcengine (火山引擎) account-level balance poller.

Reads the 火山引擎 account balance via the billing OpenAPI ``QueryBalanceAcct``
(``Service=billing``, ``Version=2022-01-01``, GET, ``Region=cn-north-1``,
host ``open.volcengineapi.com``). Auth is the Volcengine V4 request signature
(AK/SK + HMAC-SHA256) via the shared Volcengine signer. The exact wire shape
(GET / cn-north-1 / signed headers ``host;x-content-sha256;x-date``) was verified
against a live main-account key.

The secret is an access-key id/secret PAIR, accepted as
``"<access_key_id>:<access_key_secret>"`` (same convention as ``aliyun_bss``).

Balance is ACCOUNT-LEVEL: a single CNY total covering every service billed to the
火山引擎 account (语音合成 TTS / 方舟 Seedance / ...), not per-product. The prefix
therefore matches ``volcengine`` / ``volc`` / ``ark`` so one account profile
covers all of them. Per-capability spend attribution must come from the local
per-invocation ledger, not this account-level balance.
"""

from __future__ import annotations

import logging

from packages.ai.providers._volc_sigv4 import signed_headers as volc_signed_headers
from packages.core.contracts import ProviderBalanceItem

from ..base import BasePoller, money

logger = logging.getLogger(__name__)

_HOST = "open.volcengineapi.com"
_SERVICE = "billing"
_REGION = "cn-north-1"
_ACTION = "QueryBalanceAcct"
_VERSION = "2022-01-01"
_QUERY = f"Action={_ACTION}&Version={_VERSION}"

# 火山 V4 ResponseMetadata.Error codes meaning "key / permission problem" (map to
# ``unauthorized``) rather than a transient fault (map to ``error``).
_AUTH_ERROR_CODES = frozenset(
    {
        "AuthenticationError",
        "InvalidAccessKey",
        "InvalidCredential",
        "InvalidSecurityToken",
        "SignatureDoesNotMatch",
        "AccessDenied",
        "Forbidden",
        "NoPermission",
        "MissingAuthenticationToken",
    }
)


def _split_credentials(secret: str) -> tuple[str, str] | None:
    if ":" not in secret:
        return None
    access_key_id, _, secret_access_key = secret.partition(":")
    if not access_key_id or not secret_access_key:
        return None
    return access_key_id, secret_access_key


class VolcenginePoller(BasePoller):
    key = "volcengine"
    prefixes = ("volcengine", "volc", "ark")

    def query(self, profile, *, secret, client, checked_at) -> ProviderBalanceItem:
        creds = _split_credentials(secret or "")
        if creds is None:
            return self._error(
                profile,
                checked_at,
                "secret 需为 'access_key_id:access_key_secret' 形式",
            )
        access_key_id, secret_access_key = creds
        url = f"https://{_HOST}/?{_QUERY}"
        try:
            response = client.get(
                url,
                headers=volc_signed_headers(
                    access_key_id=access_key_id,
                    secret_access_key=secret_access_key,
                    method="GET",
                    url=url,
                    body=b"",
                    region=_REGION,
                    service=_SERVICE,
                ),
            )
        except Exception as exc:  # network / timeout
            return self._error(profile, checked_at, f"火山余额请求失败: {exc}", secret)

        if response.status_code in {401, 403}:
            return self._unauthorized(profile, checked_at, "火山 AK/SK 鉴权失败或无财务权限")
        try:
            data = response.json()
        except Exception:
            return self._error(profile, checked_at, f"火山响应非 JSON (HTTP {response.status_code})", secret)
        if not isinstance(data, dict):
            return self._error(profile, checked_at, "火山响应结构异常", secret)

        error = (data.get("ResponseMetadata") or {}).get("Error")
        if error:
            code = str(error.get("Code") or "unknown")
            # NEVER echo error["Message"]: Volcengine signature errors
            # (SignatureDoesNotMatch / AuthenticationError) can embed the canonical
            # request / ``Credential=<AccessKeyId>/...`` in the message. ``scrub`` does
            # not recognise the AKLT key shape, and ``.replace(secret)`` only matches the
            # full "AK:SK" pair, not the bare AK — so the Message is dropped entirely.
            # ``Code`` is a fixed enum and safe to surface for diagnosis.
            if code in _AUTH_ERROR_CODES:
                return self._unauthorized(profile, checked_at, f"火山 AK/SK 鉴权失败或无财务权限 (Code={code})")
            return self._error(profile, checked_at, f"火山 QueryBalanceAcct 失败: Code={code}", secret)

        result = data.get("Result") or {}
        available = result.get("AvailableBalance")
        if available is None:
            return self._error(profile, checked_at, "火山响应缺少 AvailableBalance", secret)
        account_id = result.get("AccountID")
        return self._ok(
            profile,
            checked_at,
            balance=money(available, "CNY"),
            detail=f"账户级总额（TTS/方舟 Seedance 等合并，不分产品）AccountID={account_id}",
        )
