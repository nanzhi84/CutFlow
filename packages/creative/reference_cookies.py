"""Reference-extractor cookie management (import / test / status).

Part B of the reference extractor: operators paste a cookie blob (browser
``Cookie:`` header, a Netscape ``cookies.txt`` export, or a JSON cookie
array). We parse any of those three into a normalised list, persist them as
a single ``Cookie:`` header string via the SecretStore (so the existing
extract path can attach them), and keep a small non-secret metadata blob for
status reporting.

OUT OF SCOPE: Playwright/browser-profile AUTO-REFRESH of douyin cookies
(see :func:`refresh_status`). This module never launches a browser.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from typing import Any, Callable

from packages.core import contracts as c
from packages.core.storage.secret_store import SecretStore

# SecretStore refs. The header value is consumed by reference_extract; the
# metadata blob is non-secret and only powers status/expiry reporting.
DOUYIN_COOKIE_SECRET_REF = "douyin_cookie"
DOUYIN_COOKIE_META_SECRET_REF = "douyin_cookie_meta"

# Cookie names that are HTTP attributes, not actual cookies (header format).
_HEADER_ATTRIBUTES = {
    "domain",
    "path",
    "expires",
    "max-age",
    "secure",
    "httponly",
    "samesite",
}

CookieFormat = str  # "auto" | "header" | "netscape" | "json"


class ReferenceCookieError(Exception):
    def __init__(self, code: c.ErrorCode, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ParsedCookie:
    name: str
    value: str
    expires: int | None = None


# --------------------------------------------------------------------------- #
# Parsing (3 formats -> the same normalised list)
# --------------------------------------------------------------------------- #


def parse_cookies(cookie_text: str, cookie_format: CookieFormat = "auto") -> list[ParsedCookie]:
    text = (cookie_text or "").strip()
    if not text:
        raise ReferenceCookieError(c.ErrorCode.reference_cookie_invalid, "Cookie content is empty.")

    parsers = _parsers_for(text, (cookie_format or "auto").strip().lower())
    errors: list[str] = []
    for parser in parsers:
        try:
            cookies = _dedupe(parser(text))
        except Exception as exc:  # noqa: BLE001 - try the next parser
            errors.append(str(exc))
            continue
        if cookies:
            return cookies
    detail = "; ".join(errors[-2:]) if errors else ""
    raise ReferenceCookieError(
        c.ErrorCode.reference_cookie_invalid,
        "Could not recognise the cookie format." + (f" ({detail})" if detail else ""),
    )


def _parsers_for(text: str, fmt: str) -> list[Callable[[str], list[ParsedCookie]]]:
    if fmt == "json":
        return [_parse_json_cookies]
    if fmt == "netscape":
        return [_parse_netscape_cookies]
    if fmt == "header":
        return [_parse_header_cookies]
    if fmt != "auto":
        raise ReferenceCookieError(c.ErrorCode.reference_cookie_invalid, f"Unsupported cookie format: {fmt}.")
    ordered: list[Callable[[str], list[ParsedCookie]]] = []
    if text.startswith(("[", "{")):
        ordered.append(_parse_json_cookies)
    if "\t" in text or text.startswith("# Netscape"):
        ordered.append(_parse_netscape_cookies)
    ordered.extend([_parse_header_cookies, _parse_json_cookies, _parse_netscape_cookies])
    # Preserve order while dropping duplicates.
    seen: set[int] = set()
    unique: list[Callable[[str], list[ParsedCookie]]] = []
    for parser in ordered:
        if id(parser) not in seen:
            seen.add(id(parser))
            unique.append(parser)
    return unique


def _parse_header_cookies(text: str) -> list[ParsedCookie]:
    cleaned = re.sub(r"(?im)^\s*cookie\s*:\s*", "", text.strip()).replace("\r", "\n")
    cookies: list[ParsedCookie] = []
    for line in cleaned.splitlines():
        for raw_part in line.split(";"):
            part = raw_part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name or name.lower() in _HEADER_ATTRIBUTES:
                continue
            cookies.append(ParsedCookie(name=name, value=value))
    return cookies


def _parse_netscape_cookies(text: str) -> list[ParsedCookie]:
    jar = MozillaCookieJar()
    payload = text if text.lstrip().startswith("# Netscape") else "# Netscape HTTP Cookie File\n" + text
    jar._really_load(io.StringIO(payload), "imported", ignore_discard=True, ignore_expires=True)  # type: ignore[attr-defined]
    cookies: list[ParsedCookie] = []
    for cookie in jar:
        if not cookie.name:
            continue
        cookies.append(ParsedCookie(name=cookie.name, value=cookie.value or "", expires=cookie.expires))
    return cookies


def _parse_json_cookies(text: str) -> list[ParsedCookie]:
    payload = json.loads(text)
    if isinstance(payload, dict):
        payload = payload.get("cookies") or payload.get("data") or payload.get("items") or []
    if not isinstance(payload, list):
        raise ReferenceCookieError(
            c.ErrorCode.reference_cookie_invalid,
            "JSON cookies must be an array, or contain a cookies/data/items array.",
        )
    cookies: list[ParsedCookie] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = str(item.get("value") or "")
        cookies.append(ParsedCookie(name=name, value=value, expires=_coerce_expiry(item)))
    return cookies


def _coerce_expiry(item: dict[str, Any]) -> int | None:
    raw = item.get("expirationDate")
    if raw is None:
        raw = item.get("expires")
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _dedupe(cookies: list[ParsedCookie]) -> list[ParsedCookie]:
    """Keep the last value seen for each cookie name, preserving order."""
    by_name: dict[str, ParsedCookie] = {}
    for cookie in cookies:
        by_name[cookie.name] = cookie
    return list(by_name.values())


def cookies_to_header(cookies: list[ParsedCookie]) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies)


# --------------------------------------------------------------------------- #
# Import / persistence
# --------------------------------------------------------------------------- #


def import_cookies(
    secret_store: SecretStore,
    *,
    cookie_text: str,
    cookie_format: CookieFormat = "auto",
    source: str | None = None,
    now: datetime | None = None,
) -> c.ReferenceCookieStatus:
    cookies = parse_cookies(cookie_text, cookie_format)
    if not cookies:
        raise ReferenceCookieError(c.ErrorCode.reference_cookie_invalid, "No cookies were recognised in the import.")
    header = cookies_to_header(cookies)
    secret_store.put(header, secret_ref=DOUYIN_COOKIE_SECRET_REF)
    updated_at = now or datetime.now(timezone.utc)
    earliest_expiry = _earliest_expiry(cookies)
    metadata = {
        "cookie_count": len(cookies),
        "earliest_expiry": earliest_expiry.isoformat() if earliest_expiry else None,
        "updated_at": updated_at.isoformat(),
        "source": source or "manual",
    }
    secret_store.put(json.dumps(metadata), secret_ref=DOUYIN_COOKIE_META_SECRET_REF)
    return cookie_status(secret_store, now=updated_at)


def _earliest_expiry(cookies: list[ParsedCookie]) -> datetime | None:
    timestamps = [cookie.expires for cookie in cookies if cookie.expires]
    if not timestamps:
        return None
    return datetime.fromtimestamp(min(timestamps), tz=timezone.utc)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


def stored_cookie_header(secret_store: SecretStore) -> str | None:
    try:
        value = secret_store.get(DOUYIN_COOKIE_SECRET_REF)
    except Exception:
        return None
    value = (value or "").strip()
    return value or None


def cookie_status(secret_store: SecretStore, *, now: datetime | None = None) -> c.ReferenceCookieStatus:
    header = stored_cookie_header(secret_store)
    if not header:
        return c.ReferenceCookieStatus(cookie_present=False, cookie_count=0)
    metadata = _load_metadata(secret_store)
    cookie_count = int(metadata.get("cookie_count") or _count_header_cookies(header))
    earliest_expiry = _parse_iso(metadata.get("earliest_expiry"))
    reference_now = now or datetime.now(timezone.utc)
    expired = bool(earliest_expiry and earliest_expiry <= reference_now)
    return c.ReferenceCookieStatus(
        cookie_present=True,
        cookie_count=cookie_count,
        earliest_expiry=earliest_expiry,
        expired=expired,
        updated_at=_parse_iso(metadata.get("updated_at")),
        source=metadata.get("source"),
    )


def _load_metadata(secret_store: SecretStore) -> dict[str, Any]:
    try:
        raw = secret_store.get(DOUYIN_COOKIE_META_SECRET_REF)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _count_header_cookies(header: str) -> int:
    return len([part for part in header.split(";") if "=" in part])


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def test_cookies(
    secret_store: SecretStore,
    *,
    request_id: str,
    url: str | None = None,
    metadata_fetch: Callable[[str, str | None], Any] | None = None,
    now: datetime | None = None,
) -> c.ReferenceCookieTestResponse:
    """Validate persisted cookies, optionally against a single video URL.

    With no URL we only confirm cookies are present. With a URL we run one
    yt-dlp metadata fetch (no media download), attaching the stored cookie
    header. ``metadata_fetch`` is injectable so unit tests never hit network.
    """
    header = stored_cookie_header(secret_store)
    status = cookie_status(secret_store, now=now)
    if not header:
        return c.ReferenceCookieTestResponse(
            success=False,
            message="No cookies are stored. Import cookies first.",
            test_url=url,
            status=status,
            request_id=request_id,
        )
    cleaned_url = (url or "").strip()
    if not cleaned_url:
        return c.ReferenceCookieTestResponse(
            success=True,
            message="Cookies are present.",
            test_url=None,
            status=status,
            request_id=request_id,
        )

    fetcher = metadata_fetch or _default_metadata_fetch
    try:
        metadata = fetcher(cleaned_url, header)
        if hasattr(metadata, "__await__"):
            metadata = await metadata
    except Exception as exc:  # noqa: BLE001 - surface as a structured failure
        message = getattr(exc, "message", None) or str(exc)
        return c.ReferenceCookieTestResponse(
            success=False,
            message=f"Cookie test failed: {message}",
            test_url=cleaned_url,
            status=status,
            request_id=request_id,
        )
    title = metadata.get("title") if isinstance(metadata, dict) else None
    return c.ReferenceCookieTestResponse(
        success=True,
        message="Cookies validated against the provided URL.",
        test_url=cleaned_url,
        title=title,
        status=status,
        request_id=request_id,
    )


async def _default_metadata_fetch(url: str, cookie_header: str | None) -> dict[str, Any]:
    from packages.creative.reference_extract import fetch_metadata

    return await fetch_metadata(url, cookie_header=cookie_header)


def refresh_status() -> dict[str, Any]:
    """Best-effort detection of the (unsupported) auto-refresh tooling.

    Auto-refresh via Playwright/browser profiles is intentionally NOT
    implemented here; we only report whether the tooling exists so the UI can
    explain why ``/refresh-cookies`` returns a not-supported response.
    """
    chrome_path = _detect_chrome_executable()
    return {
        "chrome_available": chrome_path is not None,
        "chrome_path": chrome_path,
        "playwright_available": _is_playwright_available(),
        "auto_refresh_supported": False,
    }


def _is_playwright_available() -> bool:
    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except (ImportError, ValueError):
        return False


def _detect_chrome_executable() -> str | None:
    env_path = os.environ.get("CHROME_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ):
        if os.path.exists(path):
            return path
    return None


__all__ = [
    "DOUYIN_COOKIE_SECRET_REF",
    "DOUYIN_COOKIE_META_SECRET_REF",
    "ReferenceCookieError",
    "ParsedCookie",
    "parse_cookies",
    "cookies_to_header",
    "import_cookies",
    "test_cookies",
    "stored_cookie_header",
    "cookie_status",
    "refresh_status",
]
