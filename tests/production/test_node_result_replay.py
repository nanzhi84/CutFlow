"""A node re-run after its snapshot was lost replays the provider call (issue #202).

The crash window: the vendor answered, the Gateway committed the durable row, and then
the worker died before the node's completion snapshot. A fresh attempt starts from an
EMPTY run state — a fresh Repository — and must recover the call from Postgres instead
of buying it again. TTS is the sharpest case: without the artifact being re-attached the
node does not fail, it quietly synthesizes its own audio.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from packages.ai.gateway import ProviderGateway, ProviderResult
from packages.ai.gateway.sqlalchemy_repository import SqlAlchemyProviderInvocationStore
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    Job,
    JobType,
    Money,
    NodeRun,
    NodeStatus,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.database import (
    ArtifactRow,
    JobRow,
    ProviderInvocationRow,
    UsageMeterRecordRow,
    WorkflowRunRow,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
from packages.production.pipeline.nodes import tts as tts_node
from packages.production.sqlalchemy_repository import SqlAlchemyProductionRepository

RUN_ID = "run_replay"
JOB_ID = "job_replay"
SCRIPT = "重放测试：第一句。第二句。"


class CountingTtsProvider:
    """A real TTS adapter's shape: stores the audio it produced, returns its artifact id."""

    provider_id = "acme.tts"
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
            output={
                "audio_uri": artifact.uri,
                "audio_artifact_id": artifact.id,
                "duration_sec": 1.0,
            },
            audio_seconds=1.0,
            estimated_cost=Money(amount=Decimal("0.42"), currency="CNY"),
        )


def _seed_run(db_session_factory) -> None:
    # The durable invocation row carries run_id, which is an FK into workflow_runs.
    with db_session_factory() as session:
        session.add(
            JobRow(
                id=JOB_ID,
                type=JobType.digital_human_video.value,
                status="running",
                case_id="case_demo",
                created_by="usr_admin",
                request_schema="DigitalHumanVideoRequest.v1",
                request={"case_id": "case_demo", "script": SCRIPT},
                active_run_id=RUN_ID,
            )
        )
        session.add(
            WorkflowRunRow(
                id=RUN_ID,
                job_id=JOB_ID,
                case_id="case_demo",
                workflow_template_id="digital_human_v2",
                workflow_version="v1",
                status=RunStatus.running.value,
                run_attempt=1,
                requested_by="usr_admin",
            )
        )
        session.commit()


def _tts_profile() -> ProviderProfile:
    return ProviderProfile(
        id="acme.tts.real",
        provider_id="acme.tts",
        model_id="acme-tts",
        capability="tts.speech",
        display_name="Acme TTS",
        environment="prod",
        secret_ref=None,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
    )


def _adapter(tmp_path, db_session_factory, plugin) -> LocalRuntimeAdapter:
    """A worker's runtime: a fresh (empty) run state over the shared durable stores.

    Profile resolution deliberately stays on the in-memory repository — the durable
    invocation store is the only thing under test here.
    """
    repository = Repository()
    repository.provider_profiles["acme.tts.real"] = _tts_profile()
    gateway = ProviderGateway(
        repository,
        object_store=LocalObjectStore(tmp_path / "objects"),
        auto_register_real_plugins=False,
    )
    gateway.register(plugin)
    gateway._invocation_store = SqlAlchemyProviderInvocationStore(db_session_factory)
    return LocalRuntimeAdapter(
        repository,
        provider_gateway=gateway,
        prompt_registry=PromptRegistry(repository),
        seed_media=False,
    )


def _request() -> DigitalHumanVideoRequest:
    return DigitalHumanVideoRequest(
        case_id="case_demo",
        script=SCRIPT,
        voice={"voice_id": "voice_demo_cn", "provider_profile_id": "acme.tts.real"},
    )


def _ctx(adapter: LocalRuntimeAdapter) -> NodeContext:
    request = _request()
    return NodeContext(
        adapter=adapter,
        run=WorkflowRun(
            id=RUN_ID,
            job_id=JOB_ID,
            case_id="case_demo",
            workflow_template_id="digital_human_v2",
            workflow_version="v1",
            status=RunStatus.running,
        ),
        node_run=NodeRun(
            id="nr_tts",
            run_id=RUN_ID,
            node_id="TTS",
            node_version="v1",
            status=NodeStatus.running,
            input_manifest_hash="sha256:test",
        ),
        state=RunState(request=request, artifacts={}),
    )


def _audio_artifact(output):
    return next(a for a in output.artifacts if a.kind == ArtifactKind.audio_tts)


def _snapshot(adapter: LocalRuntimeAdapter, ctx: NodeContext, db_session_factory) -> None:
    """Commit the run state the node just produced, as the node's completion does."""
    job = Job(
        id=JOB_ID,
        type=JobType.digital_human_video,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema="DigitalHumanVideoRequest.v1",
        request=_request(),
    )
    run = WorkflowRun(
        id=RUN_ID,
        job_id=JOB_ID,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    repository = adapter.repository
    repository.jobs[JOB_ID] = job
    repository.runs[RUN_ID] = run
    repository.node_runs[RUN_ID] = [ctx.node_run]
    SqlAlchemyProductionRepository(db_session_factory).sync_workflow_snapshot(
        job=job, run=run, repository=repository
    )


def test_tts_re_run_replays_the_paid_call_and_keeps_the_real_audio(
    tmp_path, db_session_factory, media_fixture_factory
):
    _seed_run(db_session_factory)
    plugin = CountingTtsProvider(media_fixture_factory.audio(duration_sec=1.0, filename="replay-node-a.wav"))
    first = tts_node.run(_ctx(_adapter(tmp_path / "w1", db_session_factory, plugin)))
    assert plugin.submit_count == 1
    original = _audio_artifact(first)

    # The snapshot never ran: a replacement worker starts from an empty run state.
    replay_adapter = _adapter(tmp_path / "w1", db_session_factory, plugin)
    replayed_output = tts_node.run(_ctx(replay_adapter))

    assert plugin.submit_count == 1
    replayed = _audio_artifact(replayed_output)
    # The decisive assertion: the audio is the vendor's, byte for byte. A node that lost
    # the provider artifact would fall through to synthesize_sandbox_tts and still look
    # green here, with a different uri and sha256.
    assert replayed.id == original.id
    assert replayed.uri == original.uri
    assert replayed.sha256 == original.sha256
    assert replayed_output.provider_invocation_ids == first.provider_invocation_ids


def test_tts_re_run_bills_the_call_once(tmp_path, db_session_factory, media_fixture_factory):
    _seed_run(db_session_factory)
    plugin = CountingTtsProvider(media_fixture_factory.audio(duration_sec=1.0, filename="replay-node-b.wav"))
    first = tts_node.run(_ctx(_adapter(tmp_path / "w1", db_session_factory, plugin)))
    invocation_id = first.provider_invocation_ids[0]

    tts_node.run(_ctx(_adapter(tmp_path / "w1", db_session_factory, plugin)))

    with db_session_factory() as session:
        usage = session.scalars(
            select(UsageMeterRecordRow).where(
                UsageMeterRecordRow.provider_invocation_id == invocation_id
            )
        ).all()
        row = session.get(ProviderInvocationRow, invocation_id)
    assert len(usage) == 1
    assert Money.model_validate(row.estimated_cost).amount == Decimal("0.42")


def test_the_paid_call_leaves_no_artifact_row_for_the_next_attempt_to_hydrate(
    tmp_path, db_session_factory, media_fixture_factory
):
    # An artifact row written by the Gateway would land in the next attempt's input
    # manifest, change the derived key, and send the re-run to the vendor with a key that
    # matches nothing — paying twice through the very mechanism meant to prevent it.
    _seed_run(db_session_factory)
    plugin = CountingTtsProvider(media_fixture_factory.audio(duration_sec=1.0, filename="replay-node-c.wav"))

    tts_node.run(_ctx(_adapter(tmp_path / "w1", db_session_factory, plugin)))

    with db_session_factory() as session:
        rows = session.scalars(select(ArtifactRow).where(ArtifactRow.run_id == RUN_ID)).all()
    assert rows == []


def test_snapshot_after_a_replay_still_bills_the_call_once(
    tmp_path, db_session_factory, media_fixture_factory
):
    # The replay puts the envelope's usage record back into the run state, and the
    # completion snapshot merges the run's usage records into Postgres. It stays one row
    # ONLY because the replayed record keeps the id the original call was billed under —
    # a freshly minted id would merge as a second row, and the bill sums per row.
    _seed_run(db_session_factory)
    plugin = CountingTtsProvider(media_fixture_factory.audio(duration_sec=1.0, filename="replay-node-d.wav"))
    tts_node.run(_ctx(_adapter(tmp_path / "w1", db_session_factory, plugin)))

    replay_adapter = _adapter(tmp_path / "w1", db_session_factory, plugin)
    replay_ctx = _ctx(replay_adapter)
    output = tts_node.run(replay_ctx)
    invocation_id = output.provider_invocation_ids[0]
    _snapshot(replay_adapter, replay_ctx, db_session_factory)

    with db_session_factory() as session:
        usage = session.scalars(
            select(UsageMeterRecordRow).where(
                UsageMeterRecordRow.provider_invocation_id == invocation_id
            )
        ).all()
        row = session.get(ProviderInvocationRow, invocation_id)
    assert len(usage) == 1
    assert Money.model_validate(row.estimated_cost).amount == Decimal("0.42")
    # And the snapshot's merge did not wipe the payload the next attempt would replay from.
    assert row.result_payload is not None
