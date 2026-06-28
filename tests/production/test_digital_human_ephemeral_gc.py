from __future__ import annotations

import pytest

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    JobStatus,
    JobType,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline import digital_human
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
from packages.production.pipeline.nodes import finalize_run_report


class RecordingDeleteStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, uri: str) -> None:
        self.deleted.append(uri)


def _artifact(kind: ArtifactKind, uri: str, *, run_id: str | None = None) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        kind=kind,
        uri=uri,
        payload_schema="uri-only",
        run_id=run_id,
    )


def _request() -> DigitalHumanVideoRequest:
    return DigitalHumanVideoRequest(
        case_id="case_demo",
        script="hello",
        voice={"voice_id": "voice_sandbox"},
    )


def _adapter_with_run(status: RunStatus) -> tuple[LocalRuntimeAdapter, WorkflowRun]:
    repository = Repository()
    workflow = object.__new__(LocalRuntimeAdapter)
    workflow.repository = repository
    job = Job(
        id="job_1",
        type=JobType.digital_human_video,
        status=JobStatus.running,
        case_id="case_demo",
        request_schema="DigitalHumanVideoRequest.v1",
        request=_request(),
    )
    run = WorkflowRun(
        id="run_1",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=status,
    )
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    repository.node_runs[run.id] = []
    return workflow, run


def _state_with_ephemerals(run_id: str | None = None) -> RunState:
    return RunState(
        request=_request(),
        artifacts={
            ArtifactKind.video_portrait_track: _artifact(
                ArtifactKind.video_portrait_track,
                "local://cutagent-ephemeral/generated-video/portrait.mp4",
                run_id=run_id,
            ),
            ArtifactKind.video_lipsync: _artifact(
                ArtifactKind.video_lipsync,
                "local://cutagent-ephemeral/generated-video/lipsync.mp4",
                run_id=run_id,
            ),
            ArtifactKind.video_rendered: _artifact(
                ArtifactKind.video_rendered,
                "local://cutagent-ephemeral/generated-video/rendered.mp4",
                run_id=run_id,
            ),
            ArtifactKind.video_final: _artifact(
                ArtifactKind.video_final,
                "local://cutagent-local/finished-video/final.mp4",
                run_id=run_id,
            ),
        },
    )


def _ephemeral_gc_event(repository: Repository):
    return next(
        event for event in repository.outbox.values() if event.topic == "workflow.run.ephemeral_gc"
    )


def _run_finalize_report(
    workflow: LocalRuntimeAdapter,
    run: WorkflowRun,
    node_run: NodeRun,
    state: RunState,
):
    return finalize_run_report.run(NodeContext(adapter=workflow, run=run, node_run=node_run, state=state))


def test_finalize_success_gc_deletes_lipsync_ephemeral_artifact(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = Repository()
    workflow = object.__new__(LocalRuntimeAdapter)
    workflow.repository = repository
    run = WorkflowRun(
        id="run_1",
        job_id="job_1",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    node_run = NodeRun(
        id="nr_finalize",
        run_id=run.id,
        node_id="FinalizeRunReport",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )
    repository.runs[run.id] = run
    repository.node_runs[run.id] = [node_run]
    state = _state_with_ephemerals()
    object_store = RecordingDeleteStore()
    monkeypatch.setattr(digital_human, "get_object_store", lambda: object_store)

    _run_finalize_report(workflow, run, node_run, state)

    assert object_store.deleted == [
        "local://cutagent-ephemeral/generated-video/portrait.mp4",
        "local://cutagent-ephemeral/generated-video/lipsync.mp4",
        "local://cutagent-ephemeral/generated-video/rendered.mp4",
    ]
    event = _ephemeral_gc_event(repository)
    assert event.payload["run_id"] == run.id
    assert event.payload["deleted_count"] == 3
    assert event.payload["skipped"] is False


def test_finalize_success_gc_keeps_ephemeral_uri_referenced_by_finished_video(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = Repository()
    workflow = object.__new__(LocalRuntimeAdapter)
    workflow.repository = repository
    run = WorkflowRun(
        id="run_1",
        job_id="job_1",
        case_id="case_demo",
        workflow_template_id="seedance_t2v_v1",
        workflow_version="v1",
        status=RunStatus.running,
    )
    node_run = NodeRun(
        id="nr_finalize",
        run_id=run.id,
        node_id="FinalizeRunReport",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )
    repository.runs[run.id] = run
    repository.node_runs[run.id] = [node_run]
    state = _state_with_ephemerals()
    shared_uri = "s3://cutagent-dev/generated-video/shared/seedance.mp4"
    state.artifacts[ArtifactKind.video_rendered] = _artifact(
        ArtifactKind.video_rendered,
        shared_uri,
    )
    state.artifacts[ArtifactKind.video_finished] = _artifact(
        ArtifactKind.video_finished,
        shared_uri,
    )
    object_store = RecordingDeleteStore()
    monkeypatch.setattr(digital_human, "get_object_store", lambda: object_store)

    _run_finalize_report(workflow, run, node_run, state)

    assert object_store.deleted == [
        "local://cutagent-ephemeral/generated-video/portrait.mp4",
        "local://cutagent-ephemeral/generated-video/lipsync.mp4",
    ]
    assert shared_uri not in object_store.deleted
    event = _ephemeral_gc_event(repository)
    assert event.payload["run_id"] == run.id
    assert event.payload["deleted_count"] == 2
    assert event.payload["skipped"] is False


def test_failed_run_retains_ephemeral_for_resume_and_keeps_committed_reservation(
    monkeypatch: pytest.MonkeyPatch,
):
    workflow, run = _adapter_with_run(RunStatus.running)
    state = _state_with_ephemerals()
    workflow.repository.reserve_selections(
        case_id="case_demo",
        run_id=run.id,
        medium="portrait",
        asset_ids=["asset_portrait_demo", "asset_portrait_alt"],
    )
    workflow.repository.commit_selection_reservation(
        run_id=run.id,
        medium="portrait",
        asset_id="asset_portrait_demo",
    )
    object_store = RecordingDeleteStore()
    monkeypatch.setattr(digital_human, "get_object_store", lambda: object_store)

    def fail_node(_node_id, _run_arg, _node_run, _state_arg):
        raise NodeExecutionError(ErrorCode.provider_remote_failed, "provider failed")

    workflow._run_node = fail_node  # type: ignore[method-assign]

    proceeded = workflow._execute_node("LipSync", run, state)

    assert proceeded is False
    assert workflow.repository.runs[run.id].status == RunStatus.failed
    # A failed run may still resume reusing its valid prefix, so its ephemeral
    # intermediates must be RETAINED (not GC'd) at the terminal hook.
    assert object_store.deleted == []
    statuses = {
        reservation.asset_id: reservation.status
        for reservation in workflow.repository.selection_reservations.values()
    }
    assert statuses["asset_portrait_demo"] == "committed"
    assert statuses["asset_portrait_alt"] == "released"
    event = _ephemeral_gc_event(workflow.repository)
    assert event.payload["run_id"] == run.id
    assert event.payload["terminal_status"] == RunStatus.failed.value
    assert event.payload["deleted_count"] == 0
    assert event.payload["skipped"] is True
    assert event.payload["retention_policy"] == "retain_for_resume"


def test_cancelled_run_gc_deletes_ephemeral_artifacts_and_writes_terminal_report(
    monkeypatch: pytest.MonkeyPatch,
):
    workflow, run = _adapter_with_run(RunStatus.running)
    for artifact in _state_with_ephemerals(run.id).artifacts.values():
        workflow.repository.artifacts[artifact.id] = artifact
    object_store = RecordingDeleteStore()
    monkeypatch.setattr(digital_human, "get_object_store", lambda: object_store)

    workflow._mark_cancelled(run.id)

    cancelled = workflow.repository.runs[run.id]
    assert cancelled.status == RunStatus.cancelled
    assert cancelled.public_report_artifact_id is not None
    assert cancelled.debug_report_artifact_id is not None
    public_report = workflow.repository.artifacts[cancelled.public_report_artifact_id]
    assert public_report.payload["status"] == RunStatus.cancelled.value
    assert object_store.deleted == [
        "local://cutagent-ephemeral/generated-video/portrait.mp4",
        "local://cutagent-ephemeral/generated-video/lipsync.mp4",
        "local://cutagent-ephemeral/generated-video/rendered.mp4",
    ]
    event = _ephemeral_gc_event(workflow.repository)
    assert event.payload["run_id"] == run.id
    assert event.payload["terminal_status"] == RunStatus.cancelled.value
    assert event.payload["deleted_count"] == 3
    assert event.payload["skipped"] is False


def test_failed_run_keeps_ephemeral_artifacts_when_debug_retention_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    workflow, run = _adapter_with_run(RunStatus.running)
    state = _state_with_ephemerals()
    object_store = RecordingDeleteStore()
    monkeypatch.setattr(digital_human, "get_object_store", lambda: object_store)
    monkeypatch.setenv("CUTAGENT_KEEP_FAILED_EPHEMERAL", "1")

    def fail_node(_node_id, _run_arg, _node_run, _state_arg):
        raise NodeExecutionError(ErrorCode.provider_remote_failed, "provider failed")

    workflow._run_node = fail_node  # type: ignore[method-assign]

    proceeded = workflow._execute_node("LipSync", run, state)

    assert proceeded is False
    assert object_store.deleted == []
    event = _ephemeral_gc_event(workflow.repository)
    assert event.payload["run_id"] == run.id
    assert event.payload["terminal_status"] == RunStatus.failed.value
    assert event.payload["deleted_count"] == 0
    assert event.payload["skipped"] is True
