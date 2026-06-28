"""Volcengine V4 request signing (HMAC-SHA256), shared across providers.

Single home for the canonical-request / string-to-sign / 4-step signing-key chain
Volcengine uses for AK/SK auth, generalized over method/url/region/service so each
caller passes its own scope. Seedance, the speech management plane, and the billing
balance poller all delegate here.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import parse_qsl, quote, urlparse


def _canonical_query(raw: str) -> str:
    # Volcengine V4 (AWS SigV4-style) requires the canonical query to be
    # RFC3986-encoded and sorted by key/value. Callers today pass either an empty
    # query or an already-sorted, special-char-free ``Action=..&Version=..``, so
    # this is a no-op for them — but it keeps the helper correct for any future
    # multi-param or special-char query instead of silently mis-signing it.
    if not raw:
        return ""
    pairs = sorted(
        (quote(key, safe="-_.~"), quote(value, safe="-_.~"))
        for key, value in parse_qsl(raw, keep_blank_values=True)
    )
    return "&".join(f"{key}={value}" for key, value in pairs)


def signed_headers(
    *,
    access_key_id: str,
    secret_access_key: str,
    method: str,
    url: str,
    body: bytes,
    region: str,
    service: str,
) -> dict[str, str]:
    """Build Volcengine V4 signed headers for one request.

    Signs ``host;x-content-sha256;x-date`` with ``payload_hash = sha256(body)``.
    The returned dict carries Host / X-Date / X-Content-Sha256 / Authorization;
    the caller adds Content-Type (it is not part of the signed set).
    """
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    query = _canonical_query(parsed.query)
    x_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    signed = "host;x-content-sha256;x-date"
    canonical_headers = f"host:{host}\nx-content-sha256:{payload_hash}\nx-date:{x_date}\n"
    canonical_request = "\n".join(
        [method.upper(), path, query, canonical_headers, signed, payload_hash]
    )
    hashed_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join(["HMAC-SHA256", x_date, scope, hashed_request])

    def _h(key: bytes, content: str) -> bytes:
        return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()

    signing_key = _h(
        _h(_h(_h(secret_access_key.encode("utf-8"), short_date), region), service),
        "request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Host": host,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": (
            f"HMAC-SHA256 Credential={access_key_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        ),
    }
