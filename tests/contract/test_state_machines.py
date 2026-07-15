import pytest

from packages.core.contracts import (
    CaseMemory,
    ErrorCode,
    JobStatus,
    NodeStatus,
    PublishItemStatus,
    PublishAttempt,
    PublishAttemptStatus,
    PublishRecord,
    ProviderStatus,
    RunStatus,
)
from packages.core.contracts.state_machines import assert_transition
from packages.core.workflow import NodeExecutionError


def test_provider_status_matches_spec_terminal_and_non_status_error_codes():
    assert {status.value for status in ProviderStatus} == {
        "prepared",
        "submitted",
        "polling",
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
    }


def test_core_state_machines_allow_spec_paths_and_reject_failed_to_running():
    assert_transition("job", JobStatus.draft, JobStatus.queued)
    assert_transition("run", RunStatus.running, RunStatus.cancelling)
    assert_transition("node", NodeStatus.pending, NodeStatus.skipped)
    assert_transition("provider", ProviderStatus.submitted, ProviderStatus.polling)

    with pytest.raises(NodeExecutionError) as exc:
        assert_transition("run", RunStatus.failed, RunStatus.running)

    assert exc.value.error.code == ErrorCode.workflow_invalid_transition


def test_start_failure_compensation_transitions_are_allowed():
    # A run/job that could not be handed to the workflow runtime is compensated
    # straight to ``failed`` (issue #69): admitted->failed and queued->failed
    # must be legal so ``_compensate_failed_start`` never forces an illegal jump.
    assert_transition("run", RunStatus.admitted, RunStatus.failed)
    assert_transition("job", JobStatus.queued, JobStatus.failed)


def test_prompt_version_draft_cannot_publish_directly():
    assert_transition("prompt_version", "draft", "reviewing")
    assert_transition("prompt_version", "reviewing", "approved")
    assert_transition("prompt_version", "approved", "published")

    with pytest.raises(NodeExecutionError):
        assert_transition("prompt_version", "draft", "approved")

    with pytest.raises(NodeExecutionError):
        assert_transition("prompt_version", "draft", "published")


def test_cancelled_upload_cannot_complete():
    assert_transition("upload_session", "prepared", "uploading")
    assert_transition("upload_session", "uploading", "cancelled")

    with pytest.raises(NodeExecutionError):
        assert_transition("upload_session", "cancelled", "completed")


def test_resumable_upload_state_machine_has_durable_recovery_boundaries():
    for current, target in (
        ("prepared", "uploading"),
        ("uploading", "completing"),
        ("completing", "object_completed"),
        ("object_completed", "verified"),
        ("verified", "ready"),
    ):
        assert_transition("upload_session", current, target)

    assert_transition("upload_session", "completing", "rejected")
    with pytest.raises(NodeExecutionError):
        assert_transition("upload_session", "ready", "verified")


def test_case_memory_only_tracks_active_constraints():
    assert {item for item in CaseMemory.model_fields["status"].annotation.__args__} == {
        "active",
        "deprecated",
        "superseded",
    }
    assert_transition("case_memory", "active", "deprecated")
    assert_transition("case_memory", "active", "superseded")

    with pytest.raises(NodeExecutionError):
        assert_transition("case_memory", "deprecated", "active")


def test_publish_item_status_uses_uploaded_initial_state_not_draft():
    assert "draft" not in {status.value for status in PublishItemStatus}
    assert_transition("publish_item", PublishItemStatus.uploaded, PublishItemStatus.normalizing)


def test_publish_attempt_contract_has_appendix_f_fields():
    attempt = PublishAttempt(
        id="attempt_1",
        batch_id="batch_1",
        item_id="item_1",
        platforms=["douyin"],
        manual_review=True,
        status=PublishAttemptStatus.manual_review_ready,
        adapter_id="sandbox.publish",
        external_task_id=None,
        results=[],
        error=None,
    )

    assert attempt.batch_id == "batch_1"
    assert attempt.platforms == ["douyin"]
    assert attempt.manual_review is True
    assert attempt.adapter_id == "sandbox.publish"
    assert attempt.finished_at is None


def test_publish_record_normalizes_legacy_item_statuses():
    failed = PublishRecord(
        id="record_failed",
        case_id="case_demo",
        platform="douyin",
        status="publish_failed",
    )
    submitted = PublishRecord(
        id="record_review",
        case_id="case_demo",
        platform="douyin",
        status="manual_review_ready",
    )

    assert failed.status == "failed"
    assert submitted.status == "submitted"
