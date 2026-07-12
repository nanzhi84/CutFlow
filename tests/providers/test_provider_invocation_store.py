"""Durable provider-invocation store (issue #193), against real Postgres.

Covers the concurrency-safe write primitives the ProviderGateway relies on for
crash recovery: idempotent create, conditional claim, immediate polling publish,
forward-only terminal writes, and the invariant that claiming never disturbs
``retry_count`` (which drives the ops cost report).
"""

from __future__ import annotations

from packages.ai.gateway.sqlalchemy_repository import SqlAlchemyProviderInvocationStore
from packages.core.contracts import ProviderError, ProviderInvocation, ProviderStatus
from packages.core.contracts.base import ErrorCode
from packages.core.provider_idempotency import build_provider_call_idempotency_key
from packages.core.storage.database import ProviderInvocationRow
from packages.core.storage.repository import new_id


def _key(slot: str = "tts") -> str:
    return build_provider_call_idempotency_key(
        job_id=new_id("job"),
        canonical_node_id="Tts",
        logical_call_slot=slot,
        provider_profile_id="profile_1",
        input_manifest_hash="manifest_1",
    )


def _prepared(idempotency_key: str) -> ProviderInvocation:
    return ProviderInvocation(
        id=new_id("pinv"),
        idempotency_key=idempotency_key,
        provider_id="acme",
        model_id="model",
        provider_profile_id="profile_1",
        capability_id="tts.speech",
        status=ProviderStatus.prepared,
    )


def test_get_or_create_is_idempotent_on_repeated_key(db_session_factory):
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    key = _key()
    first = store.get_or_create(_prepared(key))
    second = store.get_or_create(_prepared(key))

    assert first.id == second.id  # the second insert is a no-op; the first row survives
    with db_session_factory() as session:
        count = (
            session.query(ProviderInvocationRow)
            .filter(ProviderInvocationRow.idempotency_key == key)
            .count()
        )
    assert count == 1


def test_load_by_key_round_trips_and_misses_return_none(db_session_factory):
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    key = _key()
    created = store.get_or_create(_prepared(key))

    loaded = store.load_by_key(key)
    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.status is ProviderStatus.prepared
    assert store.load_by_key(_key("absent")) is None


def test_claim_submit_lets_exactly_one_winner_advance(db_session_factory):
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    invocation = store.get_or_create(_prepared(_key()))

    assert store.claim_submit(invocation.id) is True
    # A second executor racing the same prepared row loses: the row is already submitted.
    assert store.claim_submit(invocation.id) is False
    assert store.load_by_key(invocation.idempotency_key).status is ProviderStatus.submitted


def test_mark_polling_publishes_external_job_id_immediately(db_session_factory):
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    invocation = store.get_or_create(_prepared(_key()))
    store.claim_submit(invocation.id)

    store.mark_polling(invocation.id, "vendor-job-42")

    reloaded = store.load_by_key(invocation.idempotency_key)
    assert reloaded.status is ProviderStatus.polling
    assert reloaded.external_job_id == "vendor-job-42"


def test_mark_terminal_is_forward_only(db_session_factory):
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    invocation = store.get_or_create(_prepared(_key()))
    store.claim_submit(invocation.id)
    store.mark_polling(invocation.id, "vendor-job-99")

    assert store.mark_terminal(invocation.id, ProviderStatus.succeeded, None) is True
    # A second terminal write (e.g. a late writer from a superseded attempt) is a no-op.
    late_error = ProviderError(code=ErrorCode.provider_remote_failed, message="late")
    assert store.mark_terminal(invocation.id, ProviderStatus.failed, late_error) is False

    reloaded = store.load_by_key(invocation.idempotency_key)
    assert reloaded.status is ProviderStatus.succeeded
    assert reloaded.error is None


def test_claim_and_transitions_never_touch_retry_count(db_session_factory):
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    key = _key()
    invocation = store.get_or_create(_prepared(key))
    # retry_count carries the ops cost-report attempt count; the claim/CAS path must
    # leave it untouched.
    with db_session_factory() as session:
        row = session.get(ProviderInvocationRow, invocation.id)
        row.retry_count = 2
        session.commit()

    store.claim_submit(invocation.id)
    store.mark_polling(invocation.id, "vendor-job-1")
    store.mark_terminal(invocation.id, ProviderStatus.succeeded, None)

    with db_session_factory() as session:
        assert session.get(ProviderInvocationRow, invocation.id).retry_count == 2
