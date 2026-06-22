"""Unit tests for the ops balance-poller subpackage (packages.ops.balance).

Every test uses ``httpx.MockTransport`` — no real endpoint is ever hit. The
suite pins: each poller parses a mocked response into the contract; the
no-secret path returns ``unconfigured`` WITHOUT an HTTP call; MiniMax /
SDK-less Aliyun return ``unsupported``; 401 -> ``unauthorized``; failures are
scrubbed; aggregation works with no secrets; and the periodic service is OFF by
default and never fans out when disabled.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx

from packages.core.config import BalanceSettings
from packages.core.contracts import ProviderOptionsSchemaRef, ProviderProfile
from packages.ops.balance import (
    BalancePollerService,
    build_pollers,
    query_balance,
    refresh_balances,
)


class SecretStoreStub:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def get(self, secret_ref: str) -> str | None:
        return self.values.get(secret_ref)


def profile(provider_id: str, secret_ref: str | None = "provider.secret", **options) -> ProviderProfile:
    return ProviderProfile(
        id=f"{provider_id}.prod",
        provider_id=provider_id,
        model_id="model",
        capability="llm.chat",
        display_name=provider_id,
        environment="prod",
        secret_ref=secret_ref,
        default_options=options,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.options"),
    )


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test")


# --- dispatch / degradation rules ----------------------------------------

def test_missing_secret_is_unconfigured_without_http_call():
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    item = query_balance(
        profile("deepseek"),
        secret_store=SecretStoreStub(),
        client=client_for(handler),
    )
    assert item.status == "unconfigured"
    assert item.balance is None
    assert called is False


def test_unknown_provider_is_unsupported():
    item = query_balance(
        profile("totally-unknown"),
        secret_store=SecretStoreStub({"provider.secret": "x"}),
        client=client_for(lambda _: httpx.Response(200, json={})),
    )
    assert item.status == "unsupported"


def test_minimax_is_unsupported_even_with_secret_and_no_http():
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    item = query_balance(
        profile("minimax.tts"),
        secret_store=SecretStoreStub({"provider.secret": "mm-key"}),
        client=client_for(handler),
    )
    assert item.status == "unsupported"
    assert item.balance is None
    assert called is False


# --- deepseek -------------------------------------------------------------

def test_deepseek_parses_cny_balance():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user/balance"
        assert request.headers["Authorization"] == "Bearer ds-key"
        return httpx.Response(
            200,
            json={
                "is_available": True,
                "balance_infos": [
                    {"currency": "USD", "total_balance": "2.00"},
                    {"currency": "CNY", "total_balance": "12.34"},
                ],
            },
        )

    item = query_balance(
        profile("deepseek"),
        secret_store=SecretStoreStub({"provider.secret": "ds-key"}),
        client=client_for(handler),
    )
    assert item.status == "ok"
    assert item.balance is not None
    assert item.balance.amount == Decimal("12.34")
    assert item.balance.currency == "CNY"


def test_deepseek_empty_infos_is_error():
    item = query_balance(
        profile("deepseek"),
        secret_store=SecretStoreStub({"provider.secret": "ds-key"}),
        client=client_for(lambda _: httpx.Response(200, json={"balance_infos": []})),
    )
    assert item.status == "error"


def test_deepseek_honours_base_url_option():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        return httpx.Response(200, json={"balance_infos": [{"currency": "CNY", "total_balance": "1"}]})

    query_balance(
        profile("deepseek", base_url="https://relay.internal/api"),
        secret_store=SecretStoreStub({"provider.secret": "ds-key"}),
        client=client_for(handler),
    )
    assert seen["host"] == "relay.internal"


# --- kimi -----------------------------------------------------------------

def test_kimi_parses_available_balance():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/users/me/balance"
        return httpx.Response(200, json={"code": 0, "data": {"available_balance": "8.80"}})

    item = query_balance(
        profile("kimi"),
        secret_store=SecretStoreStub({"provider.secret": "kimi-key"}),
        client=client_for(handler),
    )
    assert item.status == "ok"
    assert item.balance.amount == Decimal("8.80")
    assert item.balance.currency == "CNY"


def test_kimi_nonzero_code_is_error():
    item = query_balance(
        profile("moonshot"),
        secret_store=SecretStoreStub({"provider.secret": "kimi-key"}),
        client=client_for(lambda _: httpx.Response(200, json={"code": 1})),
    )
    assert item.status == "error"


def test_kimi_401_is_unauthorized():
    item = query_balance(
        profile("kimi"),
        secret_store=SecretStoreStub({"provider.secret": "kimi-key"}),
        client=client_for(lambda _: httpx.Response(401)),
    )
    assert item.status == "unauthorized"
    assert item.balance is None


# --- heygem / runninghub --------------------------------------------------

def test_heygem_parses_coins_and_money():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/uc/openapi/accountStatus"
        return httpx.Response(200, json={"code": 0, "data": {"remainCoins": 1200, "remainMoney": "30.00"}})

    item = query_balance(
        profile("runninghub.heygem"),
        secret_store=SecretStoreStub({"provider.secret": "rh-key"}),
        client=client_for(handler),
    )
    assert item.status == "ok"
    assert item.quota_remaining == 1200.0
    assert item.unit == "coins"
    assert item.balance.amount == Decimal("30.00")


def test_heygem_http_failure_is_scrubbed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server saw rh-key")

    item = query_balance(
        profile("runninghub.heygem"),
        secret_store=SecretStoreStub({"provider.secret": "rh-key"}),
        client=client_for(handler),
    )
    assert item.status == "error"
    assert "rh-key" not in (item.detail or "")


# --- openai relay ---------------------------------------------------------

def test_openai_relay_computes_remaining():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subscription"):
            return httpx.Response(200, json={"hard_limit_usd": 100})
        return httpx.Response(200, json={"total_usage": 2500})  # cents -> $25

    item = query_balance(
        profile("openai", base_url="https://relay.test/v1"),
        secret_store=SecretStoreStub({"provider.secret": "sk-relay"}),
        client=client_for(handler),
    )
    assert item.status == "ok"
    assert item.balance.amount == Decimal("75.00")
    assert item.balance.currency == "USD"


def test_openai_relay_unlimited_reports_used_only():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subscription"):
            return httpx.Response(200, json={"hard_limit_usd": 1_000_000_000})
        return httpx.Response(200, json={"total_usage": 1234})  # $12.34

    item = query_balance(
        profile("openai", base_url="https://relay.test/v1"),
        secret_store=SecretStoreStub({"provider.secret": "sk-relay"}),
        client=client_for(handler),
    )
    assert item.status == "ok"
    assert item.balance is None
    assert "12.34" in (item.detail or "")


def test_openai_relay_html_body_is_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>", headers={"content-type": "text/html"})

    item = query_balance(
        profile("openai", base_url="https://api.openai.com/v1"),
        secret_store=SecretStoreStub({"provider.secret": "sk-relay"}),
        client=client_for(handler),
    )
    assert item.status == "error"


# --- aliyun bss (stdlib RPC v1 sign, account-level QueryAccountBalance) ----

def test_aliyun_bss_parses_account_balance():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "business.aliyuncs.com"
        assert request.url.params["Action"] == "QueryAccountBalance"
        assert "Signature" in request.url.params
        # secret never leaks into the signed query
        assert "aksecret" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "Code": "200",
                "Success": True,
                "Data": {"AvailableAmount": "1,234.56", "Currency": "CNY", "AvailableCashAmount": "1,234.56"},
            },
        )

    item = query_balance(
        profile("aliyun"),
        secret_store=SecretStoreStub({"provider.secret": "akid:aksecret"}),
        client=client_for(handler),
    )
    assert item.status == "ok"
    assert item.balance is not None
    assert item.balance.amount == Decimal("1234.56")  # comma stripped
    assert item.balance.currency == "CNY"


def test_dashscope_bearer_key_secret_is_unsupported_without_http():
    # DashScope model profiles carry a sk- Bearer key (no ':'), not an AK/SK pair:
    # they degrade quietly to unsupported instead of erroring against BSS.
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    item = query_balance(
        profile("dashscope"),
        secret_store=SecretStoreStub({"provider.secret": "sk-dashscope-model-key"}),
        client=client_for(handler),
    )
    assert item.status == "unsupported"
    assert called is False


def test_aliyun_bss_auth_error_drops_message():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Code": "InvalidAccessKeyId.NotFound", "Message": "StringToSign akid leaked", "Success": False},
        )

    item = query_balance(
        profile("aliyun"),
        secret_store=SecretStoreStub({"provider.secret": "akid:aksecret"}),
        client=client_for(handler),
    )
    assert item.status == "unauthorized"
    assert "akid" not in (item.detail or "")  # vendor Message dropped
    assert "InvalidAccessKeyId.NotFound" in (item.detail or "")


# --- volcengine (火山引擎 billing QueryBalanceAcct, hand-rolled V4 sign) ---

def test_volcengine_parses_available_balance():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "open.volcengineapi.com"
        assert request.url.params["Action"] == "QueryBalanceAcct"
        assert request.url.params["Version"] == "2022-01-01"
        # V4 signature presence + no secret leakage in the signed request
        assert request.headers["Authorization"].startswith("HMAC-SHA256 Credential=AKLT")
        assert "x-content-sha256" in request.headers
        assert "sk-secret" not in request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "ResponseMetadata": {"RequestId": "r1"},
                "Result": {"AvailableBalance": "123.45", "CashBalance": "120.00", "AccountID": 2108685169},
            },
        )

    item = query_balance(
        profile("volcengine.billing"),
        secret_store=SecretStoreStub({"provider.secret": "AKLTexample:sk-secret"}),
        client=client_for(handler),
    )
    assert item.status == "ok"
    assert item.balance is not None
    assert item.balance.amount == Decimal("123.45")
    assert item.balance.currency == "CNY"
    assert "AccountID=2108685169" in (item.detail or "")


def test_volcengine_dispatches_for_volc_and_ark_prefixes():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"AvailableBalance": "1", "AccountID": 1}})

    for provider_id in ("volc.tts", "ark.seedance"):
        item = query_balance(
            profile(provider_id),
            secret_store=SecretStoreStub({"provider.secret": "ak:sk"}),
            client=client_for(handler),
        )
        assert item.status == "ok", provider_id


def test_volcengine_bad_secret_shape_is_error_without_http():
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    item = query_balance(
        profile("volcengine.billing"),
        secret_store=SecretStoreStub({"provider.secret": "no-colon-key"}),
        client=client_for(handler),
    )
    assert item.status == "error"
    assert "access_key_id:access_key_secret" in (item.detail or "")
    assert called is False


def test_volcengine_metadata_auth_error_is_unauthorized_without_leaking_message():
    # SignatureDoesNotMatch messages can echo Credential=<AK>/... — the vendor
    # Message must NEVER reach the client-facing detail; only the Code survives.
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ResponseMetadata": {
                    "Error": {"Code": "SignatureDoesNotMatch", "Message": "Credential=AKLTleakme/... mismatch"}
                }
            },
        )

    item = query_balance(
        profile("volcengine.billing"),
        secret_store=SecretStoreStub({"provider.secret": "AKLTleakme:sk"}),
        client=client_for(handler),
    )
    assert item.status == "unauthorized"
    assert "AKLTleakme" not in (item.detail or "")
    assert "Credential=" not in (item.detail or "")
    assert "SignatureDoesNotMatch" in (item.detail or "")  # Code is safe + useful


def test_volcengine_403_is_unauthorized():
    item = query_balance(
        profile("volcengine.billing"),
        secret_store=SecretStoreStub({"provider.secret": "ak:sk"}),
        client=client_for(lambda _: httpx.Response(403)),
    )
    assert item.status == "unauthorized"


def test_volcengine_non_auth_error_drops_vendor_message():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ResponseMetadata": {"Error": {"Code": "InternalError", "Message": "boom akid:sksecret"}}},
        )

    item = query_balance(
        profile("volcengine.billing"),
        secret_store=SecretStoreStub({"provider.secret": "akid:sksecret"}),
        client=client_for(handler),
    )
    assert item.status == "error"
    assert "sksecret" not in (item.detail or "")
    assert "boom" not in (item.detail or "")  # vendor Message dropped entirely
    assert "InternalError" in (item.detail or "")


# --- aggregation ----------------------------------------------------------

def test_refresh_balances_aggregates_each_provider_with_no_secrets():
    profiles = [
        profile("deepseek", secret_ref=None),
        profile("kimi", secret_ref=None),
        profile("openai", secret_ref=None),
        profile("runninghub.heygem", secret_ref=None),
        profile("minimax", secret_ref=None),
        profile("dashscope", secret_ref=None),
    ]
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    items = refresh_balances(
        profiles,
        secret_store=SecretStoreStub(),
        client=client_for(handler),
    )
    assert len(items) == 6
    statuses = {i.status for i in items}
    assert statuses <= {"unconfigured", "unsupported"}
    assert all(i.balance is None for i in items)
    assert called is False  # no secrets -> no real network


def test_build_pollers_returns_one_per_family():
    keys = {p.key for p in build_pollers()}
    assert keys == {"deepseek", "kimi", "openai", "heygem", "minimax", "aliyun", "volcengine"}


# --- periodic service gating ---------------------------------------------

def test_service_disabled_by_default_does_not_start():
    service = BalancePollerService(
        profiles_provider=lambda: [],
        secret_store=SecretStoreStub(),
        settings=BalanceSettings(),
    )
    assert service.enabled is False
    asyncio.run(service.start())
    # No task created when disabled.
    assert service._task is None


def test_service_refresh_once_uses_provider_and_sink():
    sink: list = []
    service = BalancePollerService(
        profiles_provider=lambda: [profile("minimax", secret_ref=None)],
        secret_store=SecretStoreStub(),
        on_results=sink.append,
        settings=BalanceSettings(request_timeout_seconds=1),
    )
    results = service.refresh_once()
    assert len(results) == 1
    assert results[0].status == "unsupported"
    assert sink and sink[0] is results


def test_service_enabled_start_then_stop_runs_a_tick():
    ticks: list = []
    service = BalancePollerService(
        profiles_provider=lambda: [profile("minimax", secret_ref=None)],
        secret_store=SecretStoreStub(),
        on_results=lambda items: ticks.append(items),
        settings=BalanceSettings(poller_enabled=True, poll_interval_seconds=3600, request_timeout_seconds=1),
    )

    async def run() -> None:
        await service.start()
        for _ in range(50):
            if ticks:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(run())
    assert ticks  # at least one periodic refresh executed
    assert service._task is None
