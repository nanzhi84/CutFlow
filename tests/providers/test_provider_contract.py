from decimal import Decimal

from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway
from packages.core.contracts import (
    ErrorCode,
    Money,
    ProviderOptionsSchemaRef,
    ProviderPriceItem,
    ProviderProfile,
    ProviderStatus,
)
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore


def gateway() -> tuple[Repository, ProviderGateway]:
    repository = Repository()
    return repository, ProviderGateway(repository)


def test_provider_capability_schema_is_registered():
    repository, _ = gateway()
    capabilities = {(item.provider_id, item.capability) for item in repository.provider_capabilities.values()}
    assert ("sandbox", "tts.speech") in capabilities
    assert ("sandbox", "lipsync.video") in capabilities
    assert ("sandbox", "llm.chat") in capabilities
    assert ("minimax.tts", "tts.speech") in capabilities
    assert ("dashscope.asr", "asr.transcribe") in capabilities
    assert ("dashscope.vlm", "vlm.annotation") in capabilities
    assert ("dashscope.llm", "llm.chat") in capabilities
    assert ("runninghub.heygem", "lipsync.video") in capabilities


def test_provider_option_validation_rejects_capability_mismatch():
    _, gw = gateway()
    invocation, result = gw.invoke(
        ProviderCall(
            provider_profile_id="sandbox.tts.default",
            capability_id="lipsync.video",
            input={},
        )
    )
    assert result is None
    assert invocation.error
    assert invocation.error.code == ErrorCode.provider_unsupported_option


def test_provider_secret_missing_is_auth_failure():
    repository, gw = gateway()
    profile = ProviderProfile(
        id="sandbox.secret.tts",
        provider_id="sandbox",
        model_id="tts.local",
        capability="tts.speech",
        display_name="Secret TTS",
        environment="local",
        secret_ref="missing_secret",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
    )
    repository.provider_profiles[profile.id] = profile
    invocation, result = gw.invoke(
        ProviderCall(provider_profile_id=profile.id, capability_id="tts.speech", input={"text": "hello"})
    )
    assert result is None
    assert invocation.error
    assert invocation.error.code == ErrorCode.provider_auth_failed


def test_provider_secret_store_disable_blocks_profile(tmp_path):
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path)
    secret_ref = secret_store.put("provider-secret")
    profile = ProviderProfile(
        id="sandbox.prod.secret.tts",
        provider_id="sandbox",
        model_id="tts.local",
        capability="tts.speech",
        display_name="Prod Secret TTS",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
    )
    repository.provider_profiles[profile.id] = profile
    gw = ProviderGateway(repository, secret_store=secret_store)

    invocation, result = gw.invoke(
        ProviderCall(provider_profile_id=profile.id, capability_id="tts.speech", input={"text": "hello"})
    )
    assert result is not None
    assert invocation.status == ProviderStatus.succeeded

    secret_store.disable(secret_ref)
    blocked, blocked_result = gw.invoke(
        ProviderCall(provider_profile_id=profile.id, capability_id="tts.speech", input={"text": "hello"})
    )
    assert blocked_result is None
    assert blocked.error
    assert blocked.error.code == ErrorCode.provider_auth_failed


def test_provider_quota_timeout_and_remote_failed_are_reported():
    _, gw = gateway()
    expected = {
        "quota_exceeded": ErrorCode.provider_quota_exceeded,
        "timeout": ErrorCode.provider_timeout,
        "remote_failed": ErrorCode.provider_remote_failed,
    }
    for simulate, code in expected.items():
        invocation, result = gw.invoke(
            ProviderCall(
                provider_profile_id="sandbox.tts.default",
                capability_id="tts.speech",
                input={"text": "hello", "simulate": simulate},
            )
        )
        assert result is None
        assert invocation.error
        assert invocation.error.code == code


def test_provider_cost_unpriced_is_recorded_without_blocking_result():
    repository, gw = gateway()
    repository.price_items.clear()
    invocation, result = gw.invoke(
        ProviderCall(
            provider_profile_id="sandbox.tts.default",
            capability_id="tts.speech",
            input={"text": "hello"},
        )
    )
    assert result is not None
    assert invocation.status == ProviderStatus.succeeded
    assert invocation.billing_status == "unpriced"
    assert invocation.price_item_id is None
    usage = next(item for item in repository.usage_records.values() if item.provider_invocation_id == invocation.id)
    assert invocation.usage == usage
    assert any(alert.code == "provider.cost_unpriced" for alert in repository.alerts.values())


def test_provider_usage_is_estimated_from_price_items_when_result_has_no_cost():
    repository, gw = gateway()
    repository.price_items.clear()
    repository.price_items["price_tts_chars"] = ProviderPriceItem(
        id="price_tts_chars",
        catalog_id="price_sandbox",
        provider_id="sandbox",
        model_id="tts.local",
        capability_id="tts.speech",
        unit="input_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.01")),
    )

    invocation, result = gw.invoke(
        ProviderCall(
            provider_profile_id="sandbox.tts.default",
            capability_id="tts.speech",
            input={"text": "hello"},
        )
    )

    assert result is not None
    assert invocation.status == ProviderStatus.succeeded
    assert invocation.price_item_id == "price_tts_chars"
    assert invocation.estimated_cost.amount == Decimal("0.05")
