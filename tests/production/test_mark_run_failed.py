"""LocalRuntimeAdapter.mark_run_failed reconciles a run whose node activity was
lost to an infrastructure failure (e.g. the worker was restarted mid-node) and
so never wrote a terminal status. The run + its in-flight node land in `failed`
with a retryable `workflow.worker_lost` error so an operator can resume; the path
is idempotent and treats a mid-cancellation run as cancelled.
"""

from __future__ import annotations

from packages.core.contracts import (
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
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


def _adapter_with_run(
    run_status: RunStatus,
    *,
    node_status: NodeStatus = NodeStatus.running,
    job_status: JobStatus = JobStatus.running,
) -> tuple[LocalRuntimeAdapter, WorkflowRun, Job, NodeRun]:
    repository = Repository()
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    job = Job(
        id="job_x",
        type=JobType.digital_human_video,
        status=job_status,
        case_id="case_demo",
        request_schema="DigitalHumanVideoRequest.v1",
        request=DigitalHumanVideoRequest(
            case_id="case_demo", script="测试脚本。", voice={"voice_id": "voice_sandbox"}
        ),
    )
    run = WorkflowRun(
        id="run_x",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=run_status,
    )
    node = NodeRun(
        id="nr_lip",
        run_id=run.id,
        node_id="LipSync",
        node_version="v1",
        status=node_status,
        input_manifest_hash="h",
    )
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    repository.node_runs[run.id] = [node]
    return adapter, run, job, node


def test_mark_run_failed_fails_run_node_and_job():
    adapter, run, job, _ = _adapter_with_run(RunStatus.running)
    result = adapter.mark_run_failed(run.id, reason="worker lost")
    assert result.status == RunStatus.failed
    assert result.finished_at is not None
    node = adapter.repository.node_runs[run.id][-1]
    assert node.status == NodeStatus.failed
    assert node.error is not None
    assert node.error.code == ErrorCode.workflow_worker_lost
    assert node.error.retryable is True
    assert adapter.repository.jobs[job.id].status == JobStatus.failed


def test_mark_run_failed_releases_uncommitted_reservations():
    adapter, run, _, _ = _adapter_with_run(RunStatus.running)
    adapter.repository.reserve_selections(
        case_id="case_demo",
        run_id=run.id,
        medium="bgm",
        asset_ids=["asset_bgm_demo"],
    )

    adapter.mark_run_failed(run.id, reason="worker lost")

    assert adapter.repository.active_selection_reservations(case_id="case_demo", medium="bgm") == []
    statuses = {reservation.asset_id: reservation.status for reservation in adapter.repository.selection_reservations.values()}
    assert statuses["asset_bgm_demo"] == "released"


def test_mark_run_failed_is_idempotent():
    adapter, run, _, _ = _adapter_with_run(RunStatus.running)
    first = adapter.mark_run_failed(run.id)
    second = adapter.mark_run_failed(run.id)
    assert first.status == RunStatus.failed
    assert second.status == RunStatus.failed
    assert second.finished_at == first.finished_at


def test_mark_run_failed_completes_cancellation_when_cancelling():
    adapter, run, _, _ = _adapter_with_run(RunStatus.cancelling)
    result = adapter.mark_run_failed(run.id)
    assert result.status == RunStatus.cancelled


def test_mark_run_failed_synthesizes_next_node_when_none_running():
    # Worker died mid-LipSync: the running node was never synced, so only the
    # completed prefix is persisted. mark_run_failed must synthesize a retryable
    # failed node for the next node due to run (LipSync) so the run is resumable.
    adapter, run, _, node = _adapter_with_run(RunStatus.running, node_status=NodeStatus.succeeded)
    # the seeded node is PortraitTrackBuild (last completed before LipSync)
    adapter.repository.node_runs[run.id][0] = node.model_copy(update={"node_id": "PortraitTrackBuild"})
    # plus the rest of the completed prefix
    from packages.core.contracts import NodeRun

    prefix = [
        "ValidateRequest", "LoadCaseContext", "ResolveCreativeIntent", "TTS",
        "MaterialPackPlanning", "NarrationAlignment", "PortraitPlanning",
        "BrollPlanning", "StylePlanning", "TimelinePlanning",
    ]
    for nid in prefix:
        adapter.repository.node_runs[run.id].append(
            NodeRun(id=f"nr_{nid}", run_id=run.id, node_id=nid, node_version="v1",
                    status=NodeStatus.succeeded, input_manifest_hash="h")
        )

    result = adapter.mark_run_failed(run.id)
    assert result.status == RunStatus.failed
    synthetic = adapter.repository.node_runs[run.id][-1]
    assert synthetic.node_id == "LipSync"
    assert synthetic.status == NodeStatus.failed
    assert synthetic.error is not None and synthetic.error.retryable is True
    # completed nodes are left untouched
    assert all(
        nr.status == NodeStatus.succeeded
        for nr in adapter.repository.node_runs[run.id]
        if nr.node_id != "LipSync"
    )
