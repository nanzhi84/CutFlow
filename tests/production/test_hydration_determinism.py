"""Runtime hydration must be deterministically ordered (issue #193 follow-up).

The hydrated artifact dict is consumed last-write-wins per ArtifactKind by
``_state_from_persisted_artifacts``, and that winner lands in
``node_run.input_manifest_hash`` — which is a coordinate of the provider-call
idempotency key. A run legitimately holds several artifacts of one kind (a repair
loop writes one ``provider_raw_request`` per attempt), so an unordered scan could pick
a different winner on each hydration, mint a different key on an activity retry, and
re-submit an already-paid provider call.

The rows here are INSERTED in reverse of their ``(created_at, id)`` order, so a scan
without an ORDER BY returns them in the wrong order and the assertions fail.
"""

from __future__ import annotations

from datetime import timedelta

from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    JobType,
    RunStatus,
    utcnow,
)
from packages.core.storage.database import ArtifactRow, JobRow, WorkflowRunRow
from packages.core.storage.repository import Repository, new_id
from packages.core.workflow import manifest_hash
from packages.production import SqlAlchemyProductionRepository
from packages.production.pipeline.digital_human import LocalRuntimeAdapter

_REQUEST = DigitalHumanVideoRequest(
    case_id="case_demo",
    script="水合确定性测试。",
    voice={"voice_id": "voice_demo_cn"},
    strictness={"strict_timestamps": False},
)


def _seed_run_with_duplicate_kind_artifacts(db_session_factory) -> tuple[str, str, str]:
    """Seed a run holding two provider_raw_request artifacts (a repair loop's attempts).

    ``older`` is created first in time but inserted LAST, so the physical row order a
    seq scan returns is the opposite of the (created_at, id) order.
    """
    run_id = new_id("run")
    job_id = new_id("job")
    now = utcnow()
    older_id = "art_aaa_attempt0"
    newer_id = "art_zzz_attempt1"
    with db_session_factory() as session:
        session.add(
            JobRow(
                id=job_id,
                type=JobType.digital_human_video.value,
                status="running",
                case_id="case_demo",
                created_by="usr_admin",
                request_schema="DigitalHumanVideoRequest.v1",
                request=_REQUEST.model_dump(mode="json"),
                active_run_id=run_id,
            )
        )
        session.add(
            WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                case_id="case_demo",
                workflow_template_id="digital_human_editing_agent_v2",
                workflow_version="v1",
                status=RunStatus.running.value,
                run_attempt=1,
                requested_by="usr_admin",
            )
        )
        session.commit()

    with db_session_factory() as session:
        for artifact_id, created_at in (
            (newer_id, now),  # inserted first, but the LATER created_at
            (older_id, now - timedelta(seconds=30)),
        ):
            row = ArtifactRow(
                id=artifact_id,
                case_id="case_demo",
                run_id=run_id,
                kind=ArtifactKind.provider_raw_request.value,
                payload_schema="ProviderRawRequest.v1",
                payload={"attempt": artifact_id},
            )
            row.created_at = created_at
            row.updated_at = created_at
            session.add(row)
            session.flush()
        session.commit()
    return run_id, older_id, newer_id


def _hydrated_state(db_session_factory, run_id: str):
    repository = Repository()
    SqlAlchemyProductionRepository(db_session_factory).hydrate_workflow_runtime_snapshot(
        repository, run_id
    )
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    return adapter._state_from_persisted_artifacts(run_id, _REQUEST)


def _manifest(state, node_id: str = "LipSync") -> str:
    # The exact shape _execute_node hashes into node_run.input_manifest_hash.
    return manifest_hash(
        {
            "node_id": node_id,
            "request": _REQUEST.model_dump(mode="json"),
            "artifact_refs": {
                kind.value: artifact.id for kind, artifact in state.artifacts.items()
            },
        }
    )


def test_hydration_picks_the_latest_artifact_of_a_duplicated_kind(db_session_factory):
    run_id, older_id, newer_id = _seed_run_with_duplicate_kind_artifacts(db_session_factory)

    state = _hydrated_state(db_session_factory, run_id)

    # Last-write-wins over a (created_at, id)-ordered scan => the newest artifact wins.
    # Without the ORDER BY the reversed physical order would surface `older_id` instead.
    assert state.artifacts[ArtifactKind.provider_raw_request].id == newer_id
    assert older_id != newer_id


def test_manifest_hash_is_stable_across_hydrations(db_session_factory):
    run_id, _, _ = _seed_run_with_duplicate_kind_artifacts(db_session_factory)

    first = _manifest(_hydrated_state(db_session_factory, run_id))
    second = _manifest(_hydrated_state(db_session_factory, run_id))

    # Same rows, same manifest — so a retried activity derives the SAME idempotency key
    # and recovers the durable provider call instead of re-submitting it.
    assert first == second
