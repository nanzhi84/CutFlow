"""Aliyun BSS account-level balance poller (DashScope / Qwen / OSS share it).

The DashScope models, OSS, and every other Aliyun product bill to ONE Aliyun
account, whose balance is read account-level via the BSS ``QueryAccountBalance``
RPC. The signature (Aliyun RPC v1, HMAC-SHA1 over the sorted+percent-encoded
query) is hand-rolled over the stdlib — NO SDK dependency (consistent with the
volcengine poller).

The secret is an Aliyun access-key id/secret PAIR
("<access_key_id>:<access_key_secret>") — the SAME credential used for OSS, NOT
the DashScope model Bearer (``sk-``) key. A profile whose secret is not an AK/SK
pair (e.g. a DashScope model key) degrades to ``unsupported`` (no HTTP), so the
per-capability DashScope model profiles don't show error noise — point a dedicated
billing profile (``aliyun.billing``) at the AK/SK to surface the account balance.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import urllib.parse
import uuid
from datetime import datetime, timezone

from packages.core.contracts import ProviderBalanceItem

from ..base import BasePoller, money

logger = logging.getLogger(__name__)

_HOST = "business.aliyuncs.com"
_ACTION = "QueryAccountBalance"
_VERSION = "2017-12-14"
_DASHBOARD_HINT = "账户级总余额（OSS / DashScope 等共享，需主账号 AK/SK）"

# BSS error codes meaning "key / permission / signature problem" (-> unauthorized)
# rather than a transient fault (-> error).
_AUTH_ERROR_CODES = frozenset(
    {
        "NoPermission",
        "Forbidden",
        "Unauthorized",
        "InvalidAccessKeyId.NotFound",
        "InvalidAccessKeyId.Inactive",
        "SignatureDoesNotMatch",
        "IncompleteSignature",
        "InvalidSecurityToken.Expired",
    }
)


def _split_credentials(secret: str) -> tuple[str, str] | None:
    if ":" not in secret:
        return None
    access_key_id, _, secret_access_key = secret.partition(":")
    if not access_key_id or not secret_access_key:
        return None
    return access_key_id, secret_access_key


def _pe(value: object) -> str:
    """Aliyun RFC3986 percent-encoding (space->%20, *->%2A, ~ kept)."""
    return urllib.parse.quote(str(value), safe="~").replace("+", "%20").replace("*", "%2A")


def _signed_query(access_key_id: str, secret_access_key: str) -> str:
    params = {
        "Action": _ACTION,
        "Version": _VERSION,
        "Format": "JSON",
        "AccessKeyId": access_key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    canonical = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(params.items()))
    string_to_sign = "GET&" + _pe("/") + "&" + _pe(canonical)
    signature = base64.b64encode(
        hmac.new(
            (secret_access_key + "&").encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    return f"{canonical}&Signature={_pe(signature)}"


def _clean_amount(value: object) -> str:
    # BSS returns thousands-separated strings like "1,234.56".
    return str(value).replace(",", "") if value is not None else "0"


class AliyunBssPoller(BasePoller):
    key = "aliyun"
    prefixes = ("aliyun", "dashscope", "qwen", "bailian")

    def query(self, profile, *, secret, client, checked_at) -> ProviderBalanceItem:
        creds = _split_credentials(secret or "")
        if creds is None:
            return self._unsupported(
                profile,
                checked_at,
                "账户余额需阿里云主账号 AK/SK 对（此 profile 的 secret 不是 AK/SK，如 DashScope sk- 模型 key）",
            )
        access_key_id, secret_access_key = creds
        url = f"https://{_HOST}/?{_signed_query(access_key_id, secret_access_key)}"
        try:
            response = client.get(url)
        except Exception as exc:
            return self._error(profile, checked_at, f"BSS 请求失败: {exc}", secret)

        if response.status_code in {401, 403}:
            return self._unauthorized(profile, checked_at, _DASHBOARD_HINT)
        try:
            data = response.json()
        except Exception:
            return self._error(profile, checked_at, f"BSS 响应非 JSON (HTTP {response.status_code})", secret)
        if not isinstance(data, dict):
            return self._error(profile, checked_at, "BSS 响应结构异常", secret)

        payload = data.get("Data")
        if not isinstance(payload, dict):
            # Error envelope {"Code","Message","Success":false}. NEVER echo Message:
            # Aliyun signature errors embed the StringToSign (incl. AccessKeyId). Code
            # is a safe enum.
            code = str(data.get("Code") or "unknown")
            if code in _AUTH_ERROR_CODES:
                return self._unauthorized(profile, checked_at, f"BSS 鉴权失败 (Code={code})；{_DASHBOARD_HINT}")
            return self._error(profile, checked_at, f"BSS QueryAccountBalance 失败: Code={code}", secret)

        return self._ok(
            profile,
            checked_at,
            balance=money(_clean_amount(payload.get("AvailableAmount")), payload.get("Currency") or "CNY"),
            detail=_DASHBOARD_HINT,
        )
