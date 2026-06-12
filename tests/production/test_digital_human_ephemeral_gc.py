from __future__ import annotations

import pytest

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.repository import Repository
from packages.production.pipeline import digital_human
from packages.production.pipeline.digital_human import LocalRuntimeAdapter, RunState


class RecordingDeleteStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, uri: str) -> None:
        self.deleted.append(uri)


def _artifact(kind: ArtifactKind, uri: str) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        kind=kind,
        uri=uri,
        payload_schema="uri-only",
    )


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
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
        ),
        artifacts={
            ArtifactKind.video_portrait_track: _artifact(
                ArtifactKind.video_portrait_track,
                "local://cutagent-ephemeral/generated-video/portrait.mp4",
            ),
            ArtifactKind.video_lipsync: _artifact(
                ArtifactKind.video_lipsync,
                "local://cutagent-ephemeral/generated-video/lipsync.mp4",
            ),
            ArtifactKind.video_rendered: _artifact(
                ArtifactKind.video_rendered,
                "local://cutagent-ephemeral/generated-video/rendered.mp4",
            ),
            ArtifactKind.video_final: _artifact(
                ArtifactKind.video_final,
                "local://cutagent-local/finished-video/final.mp4",
            ),
        },
    )
    object_store = RecordingDeleteStore()
    monkeypatch.setattr(digital_human, "get_object_store", lambda: object_store)

    workflow._finalize_run_report(run, node_run, state)

    assert object_store.deleted == [
        "local://cutagent-ephemeral/generated-video/portrait.mp4",
        "local://cutagent-ephemeral/generated-video/lipsync.mp4",
        "local://cutagent-ephemeral/generated-video/rendered.mp4",
    ]
