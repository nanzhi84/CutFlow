"""Durable result replay for a re-entered succeeded call (issue #202), against real Postgres.

The window this closes: the vendor answered and was paid, then the worker died before
the node's completion snapshot. The run has no memory of the call; the durable row does.
Every case asserts the vendor SUBMIT count and the number of usage rows — a replay that
quietly re-submits, or bills twice, is the failure mode worth testing for.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, update

from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway, ProviderResult
from packages.ai.gateway.sqlalchemy_repository import SqlAlchemyProviderInvocationStore
from packages.core.contracts import (
    ArtifactKind,
    ErrorCode,
    Money,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
)
from packages.core.provider_idempotency import build_provider_call_idempotency_key
from packages.core.storage.database import ArtifactRow, ProviderInvocationRow, UsageMeterRecordRow
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository, new_id
from packages.media.assets import local_object_path


class MediaProvider:
    """Stores real audio bytes through the invocation context, like a TTS adapter."""

    provider_id = "acme"
    supports_idempotent_submit = False

    def __init__(self, audio_path):
        self.audio_bytes = audio_path.read_bytes()
        self.submit_count = 0

    def invoke_with_context(self, call, context) -> ProviderResult:
        self.submit_count += 1
        artifact = context.store_media_bytes(
            content=self.audio_bytes,
            filename="speech.wav",
            purpose="generated-audio",
            kind=ArtifactKind.audio_tts,
            call=call,
        )
        return ProviderResult(
            output={"audio_uri": artifact.uri, "audio_artifact_id": artifact.id},
            audio_seconds=1.0,
            input_tokens=12,
            estimated_cost=Money(amount=Decimal("0.37"), currency="CNY"),
        )


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


def _gateway(db_session_factory, plugin, object_store) -> ProviderGateway:
    from packages.ai.gateway.sqlalchemy_repository import SqlAlchemyProviderRuntimeRepository

    repository = Repository()
    repository.provider_profiles["profile_1"] = _profile()
    gateway = ProviderGateway(
        repository,
        provider_reader=SqlAlchemyProviderRuntimeRepository(db_session_factory),
        object_store=object_store,
        auto_register_real_plugins=False,
    )
    gateway.register(plugin)
    return gateway


def _call(key: str) -> ProviderCall:
    # run_id stays None so the durable row needs no workflow_runs FK; the Run coordinate
    # lives inside the key. Node-level replay is covered in tests/production.
    return ProviderCall(
        provider_profile_id="profile_1",
        capability_id="tts.speech",
        idempotency_key=key,
        input={"text": "重放测试"},
    )


def _key() -> str:
    return build_provider_call_idempotency_key(
        job_id=new_id("job"),
        canonical_node_id="Tts",
        logical_call_slot="tts",
        provider_profile_id="profile_1",
        input_manifest_hash="manifest_1",
    )


def _usage_rows(db_session_factory, invocation_id: str) -> list[UsageMeterRecordRow]:
    with db_session_factory() as session:
        return list(
            session.scalars(
                select(UsageMeterRecordRow).where(
                    UsageMeterRecordRow.provider_invocation_id == invocation_id
                )
            )
        )


def _row(db_session_factory, key: str) -> ProviderInvocationRow | None:
    with db_session_factory() as session:
        return session.scalar(
            select(ProviderInvocationRow).where(ProviderInvocationRow.idempotency_key == key)
        )


def _artifact_ids(db_session_factory) -> set[str]:
    with db_session_factory() as session:
        return set(session.scalars(select(ArtifactRow.id)))


def _plugin(media_fixture_factory):
    # The fixture factory is session-scoped and writes into one shared directory: a
    # filename another module already uses would silently overwrite its media.
    return MediaProvider(
        media_fixture_factory.audio(duration_sec=1.0, filename="replay-gateway.wav")
    )


def test_success_persists_result_and_bill_before_any_snapshot(
    db_session_factory, tmp_path, media_fixture_factory
):
    # The cost black hole: before #202 a crash here left a succeeded row with no result
    # and no usage row, so the money was spent and no report could see it.
    plugin = _plugin(media_fixture_factory)
    gateway = _gateway(db_session_factory, plugin, LocalObjectStore(tmp_path / "objects"))
    key = _key()

    invocation, _ = gateway.invoke(_call(key))

    row = _row(db_session_factory, key)
    assert row.status == ProviderStatus.succeeded.value
    assert row.result_payload is not None
    assert Money.model_validate(row.estimated_cost).amount == Decimal("0.37")
    usage = _usage_rows(db_session_factory, invocation.id)
    assert len(usage) == 1
    assert usage[0].audio_seconds == 1.0


def test_succeeded_row_replays_result_without_a_second_submit(
    db_session_factory, tmp_path, media_fixture_factory
):
    plugin = _plugin(media_fixture_factory)
    object_store = LocalObjectStore(tmp_path / "objects")
    first_gateway = _gateway(db_session_factory, plugin, object_store)
    key = _key()

    first, first_result = first_gateway.invoke(_call(key))
    assert plugin.submit_count == 1

    # A fresh gateway over a fresh Repository is the crashed worker's replacement: the
    # run state is gone, only the durable row survives.
    replay_gateway = _gateway(db_session_factory, plugin, object_store)
    invocation, result = replay_gateway.invoke(_call(key))

    assert plugin.submit_count == 1
    assert result is not None
    assert result.output == first_result.output
    assert result.audio_seconds == 1.0
    assert invocation.id == first.id
    assert invocation.status is ProviderStatus.succeeded
    assert invocation.error is None
    assert invocation.estimated_cost.amount == Decimal("0.37")


def test_replay_bills_the_call_exactly_once(db_session_factory, tmp_path, media_fixture_factory):
    plugin = _plugin(media_fixture_factory)
    object_store = LocalObjectStore(tmp_path / "objects")
    key = _key()
    invocation, _ = _gateway(db_session_factory, plugin, object_store).invoke(_call(key))

    for _ in range(3):
        _gateway(db_session_factory, plugin, object_store).invoke(_call(key))

    # The bill joins invocations to usage rows and sums per row: a second usage row for
    # the same invocation is a second charge on the report.
    assert len(_usage_rows(db_session_factory, invocation.id)) == 1
    row = _row(db_session_factory, key)
    assert Money.model_validate(row.estimated_cost).amount == Decimal("0.37")


def test_replay_reattaches_the_provider_artifact_to_the_re_running_node(
    db_session_factory, tmp_path, media_fixture_factory
):
    # Without this the node cannot find the provider's media and silently falls back to a
    # synthesized one (nodes/tts.py) or raises KeyError (nodes/lipsync.py).
    plugin = _plugin(media_fixture_factory)
    object_store = LocalObjectStore(tmp_path / "objects")
    key = _key()
    first_gateway = _gateway(db_session_factory, plugin, object_store)
    _, first_result = first_gateway.invoke(_call(key))
    original = first_gateway.repository.artifacts[first_result.output["audio_artifact_id"]]

    replay_gateway = _gateway(db_session_factory, plugin, object_store)
    call = _call(key).model_copy(update={"node_run_id": None, "run_id": None})
    _, result = replay_gateway.invoke(call)

    replayed = replay_gateway.repository.artifacts[result.output["audio_artifact_id"]]
    assert replayed.id == original.id
    assert replayed.uri == original.uri
    assert replayed.sha256 == original.sha256
    assert replayed.media_info.duration_sec == original.media_info.duration_sec


def test_gateway_never_writes_artifact_rows(db_session_factory, tmp_path, media_fixture_factory):
    # An artifact ROW written here would be hydrated into the next attempt's input
    # manifest, changing the derived key and defeating the de-duplication it exists for.
    # Artifacts belong to the node's completion snapshot; the Gateway only seals metadata
    # into the envelope.
    plugin = _plugin(media_fixture_factory)
    gateway = _gateway(db_session_factory, plugin, LocalObjectStore(tmp_path / "objects"))
    before = _artifact_ids(db_session_factory)

    _, result = gateway.invoke(_call(_key()))

    assert result.output["audio_artifact_id"] in gateway.repository.artifacts
    assert _artifact_ids(db_session_factory) == before


def test_replay_reopens_and_resubmits_when_the_media_was_evicted(
    db_session_factory, tmp_path, media_fixture_factory
):
    # LipSync output lives in the ephemeral tier and is collected when a run fails. A
    # dead uri handed to the node is worse than paying again, so the key is re-opened.
    plugin = _plugin(media_fixture_factory)
    object_store = LocalObjectStore(tmp_path / "objects")
    key = _key()
    first_gateway = _gateway(db_session_factory, plugin, object_store)
    first, first_result = first_gateway.invoke(_call(key))
    local_object_path(object_store, first_result.output["audio_uri"]).unlink()

    replay_gateway = _gateway(db_session_factory, plugin, object_store)
    invocation, result = replay_gateway.invoke(_call(key))

    assert plugin.submit_count == 2
    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    assert invocation.id != first.id
    # The key now belongs to the re-opened row; the paid-for original keeps its usage and
    # cost under a superseded alias so the spend stays visible.
    live = _row(db_session_factory, key)
    assert live.id == invocation.id
    assert live.retry_count == 1
    with db_session_factory() as session:
        superseded = session.get(ProviderInvocationRow, first.id)
        assert superseded.idempotency_key == f"{key}#superseded-{first.id}"
        assert superseded.status == ProviderStatus.succeeded.value
    assert len(_usage_rows(db_session_factory, first.id)) == 1
    assert len(_usage_rows(db_session_factory, invocation.id)) == 1


def test_succeeded_row_written_before_the_envelope_existed_still_rejects(
    db_session_factory, tmp_path, media_fixture_factory
):
    plugin = _plugin(media_fixture_factory)
    object_store = LocalObjectStore(tmp_path / "objects")
    key = _key()
    _gateway(db_session_factory, plugin, object_store).invoke(_call(key))
    with db_session_factory() as session:
        session.execute(
            update(ProviderInvocationRow)
            .where(ProviderInvocationRow.idempotency_key == key)
            .values(result_payload=None)
        )
        session.commit()

    invocation, result = _gateway(db_session_factory, plugin, object_store).invoke(_call(key))

    assert plugin.submit_count == 1
    assert result is None
    assert invocation.error.code is ErrorCode.idempotency_conflict


def test_mark_succeeded_declines_to_bill_a_row_a_concurrent_executor_finalized(
    db_session_factory, tmp_path, media_fixture_factory
):
    plugin = _plugin(media_fixture_factory)
    object_store = LocalObjectStore(tmp_path / "objects")
    key = _key()
    gateway = _gateway(db_session_factory, plugin, object_store)
    invocation, _ = gateway.invoke(_call(key))
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    envelope = store.load_result_envelope(invocation.id)

    # The row is already terminal, so this second finalize must write nothing at all.
    assert store.mark_succeeded(invocation.id, envelope=envelope) is False

    assert len(_usage_rows(db_session_factory, invocation.id)) == 1
