"""Volcengine (火山引擎) account-level balance poller.

Reads the 火山引擎 account balance via the billing OpenAPI ``QueryBalanceAcct``
(``Service=billing``, ``Version=2022-01-01``, GET, ``Region=cn-north-1``,
host ``open.volcengineapi.com``). Auth is the Volcengine V4 request signature
(AK/SK + HMAC-SHA256), hand-rolled with the stdlib so this poller carries NO
extra dependency (same stdlib-signing approach as ``aliyun_bss``). The exact wire
shape (GET / cn-north-1 / signed headers ``host;x-content-sha256;x-date``) was
verified against a live main-account key.

The secret is an access-key id/secret PAIR, accepted as
``"<access_key_id>:<access_key_secret>"`` (same convention as ``aliyun_bss``).

Balance is ACCOUNT-LEVEL: a single CNY total covering every service billed to the
火山引擎 account (语音合成 TTS / 方舟 Seedance / ...), not per-product. The prefix
therefore matches ``volcengine`` / ``volc`` / ``ark`` so one account profile
covers all of them. Per-capability spend attribution must come from the local
per-invocation ledger, not this account-level balance.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

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


def _signed_headers(access_key_id: str, secret_access_key: str, *, method: str = "GET", body: bytes = b"") -> dict[str, str]:
    """Build Volcengine V4 signed headers for the ``QueryBalanceAcct`` request.

    Pure stdlib (HMAC-SHA256); no SDK. Mirrors the canonical-request /
    string-to-sign / 4-step signing-key chain of the Volcengine signature spec.
    """
    x_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    signed_headers = "host;x-content-sha256;x-date"
    canonical_headers = f"host:{_HOST}\nx-content-sha256:{payload_hash}\nx-date:{x_date}\n"
    canonical_request = "\n".join([method, "/", _QUERY, canonical_headers, signed_headers, payload_hash])
    hashed_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    scope = f"{short_date}/{_REGION}/{_SERVICE}/request"
    string_to_sign = "\n".join(["HMAC-SHA256", x_date, scope, hashed_request])

    def _h(key: bytes, content: str) -> bytes:
        return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()

    signing_key = _h(_h(_h(_h(secret_access_key.encode("utf-8"), short_date), _REGION), _SERVICE), "request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Host": _HOST,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }


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
            response = client.get(url, headers=_signed_headers(access_key_id, secret_access_key))
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
