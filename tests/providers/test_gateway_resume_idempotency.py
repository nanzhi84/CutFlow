"""A resume creates a NEW run; the paid call must not be bought again (issue #202 / A1+A3).

Every case asserts the vendor SUBMIT count, not just the final status. The scenario each
one models is the real one: a run dies with a vendor task already in flight (or already
failed), an operator resumes it, and the resumed run — a brand-new ``run_id`` — re-enters
the gateway with the same Job-scoped key.

Real Postgres, because the durable row and its partial unique index are the whole subject.
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
    ProviderError,
    ProviderInvocation,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
    utcnow,
)
from packages.core.observability.telemetry import PROVIDER_CALL_REOPENS
from packages.core.provider_idempotency import build_provider_call_idempotency
from packages.core.storage.database import JobRow, ProviderInvocationRow, WorkflowRunRow
from packages.core.storage.repository import Repository, new_id
from sqlalchemy import select

_JOB_ID = "job_i202"
_MANIFEST = "manifest_1"


class ScriptedLipSync:
    """Counts submits and resumes separately — the submit count is the acceptance rule.

    ``invoke_with_context`` is the paid entrypoint; ``resume_with_context`` polls a task
    the vendor is already running (and already billing), so it costs nothing extra.
    """

    provider_id = "acme"
    supports_idempotent_submit = False

    def __init__(self, *behaviors: str, crash_resumes: int = 0):
        self.behaviors = list(behaviors) or ["succeed"]
        self.crash_resumes = crash_resumes
        self.submit_count = 0
        self.resume_count = 0

    def invoke_with_context(self, call, context) -> ProviderResult:
        behavior = self.behaviors[min(self.submit_count, len(self.behaviors) - 1)]
        self.submit_count += 1
        if behavior == "crash_after_polling":
            context.mark_polling("vendor-job-1")
            raise _WorkerDied("worker died holding a live vendor task")
        if behavior == "provider_error":
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "vendor rejected")
        if behavior == "timeout":
            raise ProviderRuntimeError(ErrorCode.provider_timeout, "vendor timed out")
        return ProviderResult(output={"video_uri": "s3://out.mp4"})

    def resume_with_context(self, call, context, external_job_id) -> ProviderResult:
        self.resume_count += 1
        if self.resume_count <= self.crash_resumes:
            raise _WorkerDied("worker died again, still holding the same vendor task")
        return ProviderResult(output={"video_uri": f"s3://out-{external_job_id}.mp4"})


class _WorkerDied(Exception):
    """Not a ProviderRuntimeError: the gateway writes no terminal state, exactly as when
    the process is killed. The durable row is left where it was — 'polling'."""


def _profile() -> ProviderProfile:
    return ProviderProfile(
        id="profile_1",
        provider_id="acme",
        model_id="model",
        capability="lipsync.video",
        display_name="Acme",
        environment="prod",
        secret_ref=None,
        timeout_sec=30,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="acme.options"),
    )


@pytest.fixture
def gateway(db_session_factory):
    repository = Repository()
    profile = _profile()
    repository.provider_profiles[profile.id] = profile
    gateway = ProviderGateway(
        repository,
        provider_reader=SqlAlchemyProviderRuntimeRepository(db_session_factory),
        auto_register_real_plugins=False,
    )
    return gateway


def _make_run(db_session_factory, run_id: str, *, job_id: str = _JOB_ID) -> str:
    """Persist the job (once) and a run of it: provider_invocations.run_id is a real FK."""
    with db_session_factory() as session:
        if session.get(JobRow, job_id) is None:
            session.add(
                JobRow(
                    id=job_id,
                    type="digital_human_video",
                    status="running",
                    request_schema="DigitalHumanVideoRequest.v1",
                    request={},
                )
            )
        session.add(
            WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                workflow_template_id="digital_human_v2",
                workflow_version="v2",
                status="running",
            )
        )
        session.commit()
    return run_id


def _call(run_id: str, *, job_id: str = _JOB_ID, slot: str = "lipsync") -> ProviderCall:
    identity = build_provider_call_idempotency(
        job_id=job_id,
        run_id=run_id,
        canonical_node_id="LipSync",
        logical_call_slot=slot,
        provider_profile_id="profile_1",
        input_manifest_hash=_MANIFEST,
    )
    return ProviderCall(
        run_id=run_id,
        provider_profile_id="profile_1",
        capability_id="lipsync.video",
        idempotency_key=identity.key,
        fallback_idempotency_keys=list(identity.fallback_keys),
        input={},
    )


def _rows_for_key(db_session_factory, key: str) -> list[ProviderInvocationRow]:
    with db_session_factory() as session:
        return list(
            session.scalars(
                select(ProviderInvocationRow).where(ProviderInvocationRow.idempotency_key == key)
            )
        )


def test_resume_polls_the_in_flight_task_instead_of_submitting_again(gateway, db_session_factory):
    # T1. Run A published a vendor task id and died. The operator resumes: run B is a new
    # run, but the same job, node, slot, profile and prefix artifacts — so the same key.
    plugin = ScriptedLipSync("crash_after_polling")
    gateway.register(plugin)
    run_a = _make_run(db_session_factory, new_id("run"))
    run_b = _make_run(db_session_factory, new_id("run"))

    with pytest.raises(_WorkerDied):
        gateway.invoke(_call(run_a))
    assert plugin.submit_count == 1

    invocation, result = gateway.invoke(_call(run_b))

    assert plugin.submit_count == 1, "the resumed run re-submitted (and re-paid for) a live task"
    assert plugin.resume_count == 1
    assert result is not None
    assert result.output["video_uri"] == "s3://out-vendor-job-1.mp4"
    assert invocation.status is ProviderStatus.succeeded
    # One durable identity, not two: the key is what makes the task findable.
    rows = _rows_for_key(db_session_factory, _call(run_b).idempotency_key)
    assert len(rows) == 1
    assert rows[0].run_id == run_a, "the run that paid keeps the invocation (and its bill)"


def test_three_deep_resume_chain_still_lands_on_the_first_runs_task(gateway, db_session_factory):
    # T3. A -> resume -> B -> resume -> C, each hop dying on the same live vendor task.
    # Hydration only ever loads ONE resume hop, so a "chain root run id" coordinate would
    # be unrecoverable from C; job_id is constant for all three by construction.
    plugin = ScriptedLipSync("crash_after_polling", crash_resumes=1)
    gateway.register(plugin)
    run_a = _make_run(db_session_factory, new_id("run"))
    run_b = _make_run(db_session_factory, new_id("run"))
    run_c = _make_run(db_session_factory, new_id("run"))

    with pytest.raises(_WorkerDied):
        gateway.invoke(_call(run_a))
    with pytest.raises(_WorkerDied):
        gateway.invoke(_call(run_b))

    assert _call(run_c).idempotency_key == _call(run_a).idempotency_key
    _, result = gateway.invoke(_call(run_c))

    assert plugin.submit_count == 1, "a resume hop re-bought the task the chain root paid for"
    assert plugin.resume_count == 2
    assert result is not None
    assert len(_rows_for_key(db_session_factory, _call(run_c).idempotency_key)) == 1


def test_same_run_re_entry_of_a_failed_call_is_not_re_opened(gateway, db_session_factory):
    # Infrastructure retrying the SAME run: the vendor failure already propagated out of
    # the node and failed the run, so this is a lost-completion replay, not a new decision.
    # Re-opening here would re-bill a failure the run has already acted on.
    plugin = ScriptedLipSync("provider_error")
    gateway.register(plugin)
    run_a = _make_run(db_session_factory, new_id("run"))

    first, _ = gateway.invoke(_call(run_a))
    assert first.status is ProviderStatus.failed

    invocation, result = gateway.invoke(_call(run_a))

    assert plugin.submit_count == 1
    assert result is None
    assert invocation.status is ProviderStatus.failed


def test_resume_re_opens_a_terminally_failed_call_and_reaches_the_vendor(
    gateway, db_session_factory
):
    # T4. The other half of the pair: the vendor genuinely FAILED (no task in flight), so
    # "the vendor 5xx'd, try again" — the most common reason to resume at all — must
    # actually reach the vendor. Answering the resumed run with the stored error forever
    # would make resume a no-op. This one is SUPPOSED to submit twice.
    plugin = ScriptedLipSync("timeout", "succeed")
    gateway.register(plugin)
    run_a = _make_run(db_session_factory, new_id("run"))
    run_b = _make_run(db_session_factory, new_id("run"))

    failed, _ = gateway.invoke(_call(run_a))
    assert failed.status is ProviderStatus.timed_out
    assert plugin.submit_count == 1

    invocation, result = gateway.invoke(_call(run_b))

    assert plugin.submit_count == 2
    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    assert invocation.id != failed.id
    # The key holds exactly one live row — the reopened one — and the retired row keeps its
    # id, its status and the bill it ran up, under a superseded alias.
    key = _call(run_b).idempotency_key
    live = _rows_for_key(db_session_factory, key)
    assert len(live) == 1
    assert live[0].id == invocation.id
    assert live[0].retry_count == 1
    with db_session_factory() as session:
        retired = session.get(ProviderInvocationRow, failed.id)
        assert retired.idempotency_key == f"{key}#superseded-{failed.id}"
        assert retired.status == ProviderStatus.timed_out.value


def test_re_opening_an_unresolved_submit_is_counted_and_logged(
    gateway, db_session_factory, caplog
):
    # The one path in the gateway that CAN double charge: the prior attempt never learned
    # whether its submit reached the vendor, so the vendor may have accepted (and be
    # billing) it. Re-opening is the accepted price of a working resume, but it must be
    # visible — a silent double charge is unauditable.
    plugin = ScriptedLipSync("succeed")
    gateway.register(plugin)
    run_a = _make_run(db_session_factory, new_id("run"))
    run_b = _make_run(db_session_factory, new_id("run"))
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    row = _seed_durable_row(store, _call(run_a))
    store.mark_terminal(
        row.id,
        ProviderStatus.timed_out,
        ProviderError(
            code=ErrorCode.provider_submit_outcome_unknown,
            message="Provider submit outcome is unknown after an interrupted attempt.",
            retryable=False,
        ),
    )
    before = _reopen_count("submit_outcome_unknown")

    with caplog.at_level("WARNING", logger="packages.ai.gateway.provider_gateway"):
        _, result = gateway.invoke(_call(run_b))

    assert result is not None
    assert plugin.submit_count == 1  # the reopened call's first (and only) submit
    assert _reopen_count("submit_outcome_unknown") == before + 1
    assert any("may pay for it twice" in record.getMessage() for record in caplog.records)


def _seed_durable_row(store, call: ProviderCall):
    """The durable row a crashed attempt leaves behind, driven through the store's own API."""
    store.get_or_create(
        ProviderInvocation(
            id=new_id("pinv"),
            run_id=call.run_id,
            idempotency_key=call.idempotency_key,
            provider_id="acme",
            model_id="model",
            provider_profile_id="profile_1",
            capability_id="lipsync.video",
            status=ProviderStatus.prepared,
            started_at=utcnow(),
        )
    )
    row = store.load_by_key(call.idempotency_key)
    assert store.claim_submit(row.id)
    return row


def _reopen_count(reason: str) -> float:
    return PROVIDER_CALL_REOPENS.labels(reason=reason)._value.get()  # noqa: SLF001


def test_a_task_in_flight_under_the_superseded_key_scheme_is_recovered_not_resubmitted(
    gateway, db_session_factory
):
    # Deploy window: the task was submitted by the OLD binary under the run-scoped v1 key,
    # then the worker restarted onto the new one. Without the read-only fallback lookup the
    # new key finds nothing, the gateway opens a second identity and buys the task twice.
    plugin = ScriptedLipSync("succeed")
    gateway.register(plugin)
    run_a = _make_run(db_session_factory, new_id("run"))
    call = _call(run_a)
    legacy_key = call.fallback_idempotency_keys[0]
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    row = _seed_durable_row(
        store, call.model_copy(update={"idempotency_key": legacy_key})
    )
    store.mark_polling(row.id, "vendor-job-legacy")

    invocation, result = gateway.invoke(call)

    assert plugin.submit_count == 0, "the in-flight task was bought a second time"
    assert plugin.resume_count == 1
    assert result is not None
    assert invocation.id == row.id
    # Read-only: the recovered row keeps its old key, and no row was opened under the new one.
    assert _rows_for_key(db_session_factory, call.idempotency_key) == []
    assert len(_rows_for_key(db_session_factory, legacy_key)) == 1


def test_a_fresh_call_opens_its_row_under_the_current_key_never_the_fallback(
    gateway, db_session_factory
):
    plugin = ScriptedLipSync("succeed")
    gateway.register(plugin)
    run_a = _make_run(db_session_factory, new_id("run"))
    call = _call(run_a)

    invocation, _ = gateway.invoke(call)

    assert invocation.idempotency_key == call.idempotency_key
    assert len(_rows_for_key(db_session_factory, call.idempotency_key)) == 1
    assert _rows_for_key(db_session_factory, call.fallback_idempotency_keys[0]) == []
