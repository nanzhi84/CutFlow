import pytest
from pydantic import ValidationError

from packages.core.contracts import (
    DigitalHumanVideoRequest,
    Job,
    JobStatus,
    JobType,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)


def test_job_request_uses_schema_version_discriminator_and_spec_field_names():
    job = Job.model_validate(
        {
            "id": "job_1",
            "type": "digital_human_video",
            "case_id": "case_demo",
            "created_by": "usr_admin",
            "status": "draft",
            "request_schema": "digital_human_video_request.v1",
            "request": {
                "schema_version": "digital_human_video_request.v1",
                "case_id": "case_demo",
                "script": "hello",
                "voice": {"voice_id": "voice_sandbox"},
            },
            "active_run_id": None,
        }
    )

    assert job.type is JobType.digital_human_video
    assert job.status is JobStatus.draft
    assert job.created_by == "usr_admin"
    assert job.version == 1
    assert job.request_schema == "digital_human_video_request.v1"
    assert isinstance(job.request, DigitalHumanVideoRequest)
    assert job.request.schema_version == "digital_human_video_request.v1"
    assert job.active_run_id is None
    assert job.latest_finished_video_id is None


def test_workflow_run_uses_requested_by_retry_of_and_experiment_assignment():
    run = WorkflowRun(
        id="run_1",
        job_id="job_1",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.created,
        requested_by="usr_admin",
        retry_of_run_id="run_prev",
        experiment_assignment_id="exp_1",
    )

    assert run.requested_by == "usr_admin"
    assert run.retry_of_run_id == "run_prev"
    assert run.experiment_assignment_id == "exp_1"


def test_node_run_requires_input_manifest_hash_and_tracks_attempt_reasons():
    with pytest.raises(ValidationError):
        NodeRun(
            id="node_missing_input",
            run_id="run_1",
            node_id="ResolveCreativeIntent",
            node_version="v1",
            status=NodeStatus.pending,
        )

    node = NodeRun(
        id="node_1",
        run_id="run_1",
        node_id="ResolveCreativeIntent",
        node_version="v1",
        status=NodeStatus.pending,
        input_manifest_hash="sha256:abc",
        attempt=2,
        skipped_reason="upstream cached",
        degradation_reason="timestamp estimated",
    )

    assert node.attempt == 2
    assert node.input_manifest_hash == "sha256:abc"
    assert node.skipped_reason == "upstream cached"
    assert node.degradation_reason == "timestamp estimated"
