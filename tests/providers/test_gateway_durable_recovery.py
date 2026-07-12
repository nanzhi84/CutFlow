"""Gateway durable idempotency recovery table (issue #193), against real Postgres.

Every case asserts the vendor SUBMIT count, not just the final status — the issue's
acceptance rule. A scripted fake adapter simulates a worker crash at each window
(after claim / after task id), then a fresh gateway call recovers from the durable
row instead of re-submitting.
"""

from __future__ import annotations

import pytest

from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderGateway,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.gateway.sqlalchemy_repository import (
    SqlAlchemyProviderInvocationStore,
    SqlAlchemyProviderRuntimeRepository,
)
from packages.core.contracts import (
    ErrorCode,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
)
from packages.core.provider_idempotency import build_provider_call_idempotency_key
from packages.core.storage.repository import Repository, new_id


class _SimulatedCrash(Exception):
    """Not a ProviderRuntimeError, so the gateway does NOT mark a terminal state —
    it models a worker dying mid-invoke, leaving the durable row where it was."""


class ScriptedProvider:
    provider_id = "acme"

    def __init__(self, *behaviors: str, supports_idempotent_submit: bool = False):
        self.behaviors = list(behaviors) or ["succeed"]
        self.submit_count = 0
        self.supports_idempotent_submit = supports_idempotent_submit

    def invoke_with_context(self, call, context) -> ProviderResult:
        behavior = self.behaviors[min(self.submit_count, len(self.behaviors) - 1)]
        self.submit_count += 1
        if behavior == "crash_after_submit":
            raise _SimulatedCrash("worker died before publishing task id")
        if behavior == "crash_after_polling":
            context.mark_polling("vendor-job-poll")
            raise _SimulatedCrash("worker died after publishing task id")
        if behavior == "provider_error":
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "vendor rejected")
        if behavior == "succeed":
            return ProviderResult(output={"ok": True})
        raise AssertionError(f"unknown behavior {behavior}")


def _profile() -> ProviderProfile:
    return ProviderProfile(
        id="profile_1",
        provider_id="acme",
        model_id="model",
        capability="tts.speech",
        display_name="Acme",
        environment="prod",
        secret_ref=None,
        timeout_sec=30,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="acme.options"),
    )


def _gateway(db_session_factory, plugin) -> ProviderGateway:
    repository = Repository()
    profile = _profile()
    repository.provider_profiles[profile.id] = profile
    gateway = ProviderGateway(
        repository,
        provider_reader=SqlAlchemyProviderRuntimeRepository(db_session_factory),
        auto_register_real_plugins=False,
    )
    gateway.register(plugin)
    return gateway


def _call(idempotency_key: str | None) -> ProviderCall:
    # call.run_id stays None so the durable row has no workflow_runs FK to satisfy; the
    # Run coordinate lives inside the helper key instead.
    return ProviderCall(
        provider_profile_id="profile_1",
        capability_id="tts.speech",
        idempotency_key=idempotency_key,
        input={},
    )


def _key(slot: str = "tts") -> str:
    return build_provider_call_idempotency_key(
        run_id=new_id("run"),
        canonical_node_id="Tts",
        logical_call_slot=slot,
        provider_profile_id="profile_1",
        input_manifest_hash="manifest_1",
    )


def test_fresh_helper_key_submits_once_and_persists_durably(db_session_factory):
    plugin = ScriptedProvider("succeed")
    gateway = _gateway(db_session_factory, plugin)
    key = _key()

    invocation, result = gateway.invoke(_call(key))

    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    assert plugin.submit_count == 1
    durable = SqlAlchemyProviderInvocationStore(db_session_factory).load_by_key(key)
    assert durable is not None
    assert durable.status is ProviderStatus.succeeded


def test_non_helper_key_stays_transient_without_durable_row(db_session_factory):
    plugin = ScriptedProvider("succeed")
    gateway = _gateway(db_session_factory, plugin)
    legacy_key = "run_x:node_x:tts"

    invocation, result = gateway.invoke(_call(legacy_key))

    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    # The transient path never writes the idempotency_key column / a durable row.
    assert invocation.idempotency_key is None
    assert SqlAlchemyProviderInvocationStore(db_session_factory).load_by_key(legacy_key) is None


def test_recover_polling_row_does_not_resubmit(db_session_factory):
    plugin = ScriptedProvider("crash_after_polling", "succeed")
    gateway = _gateway(db_session_factory, plugin)
    key = _key()

    with pytest.raises(_SimulatedCrash):
        gateway.invoke(_call(key))
    assert plugin.submit_count == 1
    assert SqlAlchemyProviderInvocationStore(db_session_factory).load_by_key(key).status is (
        ProviderStatus.polling
    )

    invocation, result = gateway.invoke(_call(key))

    # Stage A placeholder: surfaces the polling row without a second submit.
    assert plugin.submit_count == 1
    assert invocation.status is ProviderStatus.polling
    assert result is None


def test_recover_submitted_resubmits_when_adapter_is_idempotent(db_session_factory):
    plugin = ScriptedProvider(
        "crash_after_submit", "succeed", supports_idempotent_submit=True
    )
    gateway = _gateway(db_session_factory, plugin)
    key = _key()

    with pytest.raises(_SimulatedCrash):
        gateway.invoke(_call(key))
    assert plugin.submit_count == 1
    assert SqlAlchemyProviderInvocationStore(db_session_factory).load_by_key(key).status is (
        ProviderStatus.submitted
    )

    invocation, result = gateway.invoke(_call(key))

    # Vendor de-dups by key, so a same-key resubmit is safe and completes the call.
    assert plugin.submit_count == 2
    assert invocation.status is ProviderStatus.succeeded
    assert result is not None


def test_recover_submitted_stops_when_adapter_not_idempotent(db_session_factory):
    plugin = ScriptedProvider("crash_after_submit", supports_idempotent_submit=False)
    gateway = _gateway(db_session_factory, plugin)
    key = _key()

    with pytest.raises(_SimulatedCrash):
        gateway.invoke(_call(key))
    assert plugin.submit_count == 1

    invocation, result = gateway.invoke(_call(key))

    # Outcome unknown: do not resubmit; fail with the explicit code instead.
    assert plugin.submit_count == 1
    assert result is None
    assert invocation.status is ProviderStatus.timed_out
    assert invocation.error is not None
    assert invocation.error.code is ErrorCode.provider_submit_outcome_unknown
    assert SqlAlchemyProviderInvocationStore(db_session_factory).load_by_key(key).status is (
        ProviderStatus.timed_out
    )


def test_recover_succeeded_row_rejects_without_calling_provider(db_session_factory):
    plugin = ScriptedProvider("succeed")
    gateway = _gateway(db_session_factory, plugin)
    key = _key()

    gateway.invoke(_call(key))
    assert plugin.submit_count == 1

    invocation, result = gateway.invoke(_call(key))

    assert plugin.submit_count == 1
    assert result is None
    assert invocation.error is not None
    assert invocation.error.code is ErrorCode.idempotency_conflict


def test_recover_failed_row_does_not_open_new_task(db_session_factory):
    plugin = ScriptedProvider("provider_error")
    gateway = _gateway(db_session_factory, plugin)
    key = _key()

    first, _ = gateway.invoke(_call(key))
    assert first.status is ProviderStatus.failed
    assert plugin.submit_count == 1

    invocation, result = gateway.invoke(_call(key))

    assert plugin.submit_count == 1
    assert result is None
    assert invocation.status is ProviderStatus.failed
