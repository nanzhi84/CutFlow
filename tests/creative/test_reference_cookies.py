"""Reference-extractor cookie management (Part B): parsers + persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import anyio
import pytest

from packages.core import contracts as c
from packages.creative import reference_cookies as rc

HEADER_BLOB = "Cookie: sessionid=abc123; ttwid=xyz789; passport_csrf_token=tok"
NETSCAPE_BLOB = (
    "# Netscape HTTP Cookie File\n"
    ".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc123\n"
    ".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\txyz789\n"
    ".douyin.com\tTRUE\t/\tTRUE\t0\tpassport_csrf_token\ttok\n"
)
JSON_BLOB = (
    '[{"name":"sessionid","value":"abc123"},'
    '{"name":"ttwid","value":"xyz789"},'
    '{"name":"passport_csrf_token","value":"tok"}]'
)
EXPECTED = {("sessionid", "abc123"), ("ttwid", "xyz789"), ("passport_csrf_token", "tok")}


class _MemorySecretStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, plaintext: str, *, secret_ref: str | None = None) -> str:
        ref = secret_ref or "sec"
        self._values[ref] = plaintext
        return ref

    def get(self, secret_ref: str) -> str | None:
        return self._values.get(secret_ref)

    def disable(self, secret_ref: str) -> None:
        self._values.pop(secret_ref, None)


@pytest.mark.parametrize(
    ("fmt", "blob"),
    [("auto", HEADER_BLOB), ("auto", NETSCAPE_BLOB), ("auto", JSON_BLOB), ("header", HEADER_BLOB), ("netscape", NETSCAPE_BLOB), ("json", JSON_BLOB)],
)
def test_all_three_formats_parse_to_the_same_cookie_set(fmt: str, blob: str) -> None:
    cookies = rc.parse_cookies(blob, fmt)
    assert {(cookie.name, cookie.value) for cookie in cookies} == EXPECTED


def test_cookies_to_header_round_trips() -> None:
    cookies = rc.parse_cookies(HEADER_BLOB, "auto")
    header = rc.cookies_to_header(cookies)
    reparsed = rc.parse_cookies(header, "header")
    assert {(c2.name, c2.value) for c2 in reparsed} == EXPECTED


def test_parse_rejects_empty_and_unrecognised_text() -> None:
    with pytest.raises(rc.ReferenceCookieError) as empty:
        rc.parse_cookies("", "auto")
    assert empty.value.code == c.ErrorCode.reference_cookie_invalid

    with pytest.raises(rc.ReferenceCookieError):
        rc.parse_cookies("not a cookie at all", "json")


def test_json_supports_expiry_and_wrapped_arrays() -> None:
    blob = '{"cookies":[{"name":"a","value":"1","expirationDate":1700000000.5}]}'
    cookies = rc.parse_cookies(blob, "auto")
    assert cookies == [rc.ParsedCookie(name="a", value="1", expires=1700000000)]


def test_import_persists_header_and_status_reports_metadata() -> None:
    store = _MemorySecretStore()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = int((now + timedelta(days=30)).timestamp())
    blob = f'[{{"name":"sessionid","value":"abc","expirationDate":{future}}}]'

    status = rc.import_cookies(store, cookie_text=blob, cookie_format="json", source="paste", now=now)
    assert status.cookie_present is True
    assert status.cookie_count == 1
    assert status.source == "paste"
    assert status.expired is False

    # The persisted secret is a usable Cookie header.
    assert store.get(rc.DOUYIN_COOKIE_SECRET_REF) == "sessionid=abc"

    reloaded = rc.cookie_status(store, now=now)
    assert reloaded.cookie_present is True
    assert reloaded.earliest_expiry is not None


def test_status_marks_expired_cookies() -> None:
    store = _MemorySecretStore()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    past = int((now - timedelta(days=1)).timestamp())
    blob = f'[{{"name":"sessionid","value":"abc","expirationDate":{past}}}]'
    rc.import_cookies(store, cookie_text=blob, cookie_format="json", now=now)
    status = rc.cookie_status(store, now=now)
    assert status.expired is True


def test_status_absent_when_no_cookie() -> None:
    status = rc.cookie_status(_MemorySecretStore())
    assert status.cookie_present is False
    assert status.cookie_count == 0


def test_test_cookies_without_url_only_checks_presence() -> None:
    store = _MemorySecretStore()
    # No cookies yet.
    missing = anyio.run(lambda: rc.test_cookies(store, request_id="req_1"))
    assert missing.success is False

    rc.import_cookies(store, cookie_text=HEADER_BLOB, cookie_format="header")
    present = anyio.run(lambda: rc.test_cookies(store, request_id="req_2"))
    assert present.success is True
    assert present.test_url is None


def test_test_cookies_with_url_uses_injected_fetch() -> None:
    store = _MemorySecretStore()
    rc.import_cookies(store, cookie_text=HEADER_BLOB, cookie_format="header")
    seen: dict[str, str | None] = {}

    async def fake_fetch(url: str, cookie_header: str | None) -> dict[str, str]:
        seen["url"] = url
        seen["cookie"] = cookie_header
        return {"title": "Benchmark Clip"}

    response = anyio.run(
        lambda: rc.test_cookies(
            store,
            url="https://www.douyin.com/video/123",
            metadata_fetch=fake_fetch,
            request_id="req_3",
        )
    )
    assert response.success is True
    assert response.title == "Benchmark Clip"
    assert seen["url"] == "https://www.douyin.com/video/123"
    assert seen["cookie"] == "sessionid=abc123; ttwid=xyz789; passport_csrf_token=tok"


def test_test_cookies_surfaces_fetch_failure_as_structured_result() -> None:
    store = _MemorySecretStore()
    rc.import_cookies(store, cookie_text=HEADER_BLOB, cookie_format="header")

    async def failing_fetch(url: str, cookie_header: str | None) -> dict[str, str]:
        raise rc.ReferenceCookieError(c.ErrorCode.reference_unreachable, "expired session")

    response = anyio.run(
        lambda: rc.test_cookies(
            store,
            url="https://www.douyin.com/video/123",
            metadata_fetch=failing_fetch,
            request_id="req_4",
        )
    )
    assert response.success is False
    assert "expired session" in response.message


def test_refresh_status_reports_unsupported() -> None:
    status = rc.refresh_status()
    assert status["auto_refresh_supported"] is False
    assert set(status) == {"chrome_available", "chrome_path", "playwright_available", "auto_refresh_supported"}
