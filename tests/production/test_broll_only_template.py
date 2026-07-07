from __future__ import annotations

import pytest

from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import (
    LocalRuntimeAdapter,
    broll_only_template,
    template_for,
)
from packages.production.pipeline.node_sequence import BROLL_ONLY_SEQUENCE, expected_node_count
from packages.production.pipeline.nodes import validate_request


def _output_kinds_by_node(template):
    return {spec.node_id: list(spec.output_artifact_kinds) for spec in template.nodes}


def _validate_ctx(request: DigitalHumanVideoRequest) -> NodeContext:
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = Repository()
    run = WorkflowRun(
        id="run_validate",
        job_id="job_validate",
        case_id=request.case_id,
        workflow_template_id=request.workflow_template_id,
        workflow_version="v1",
        status=RunStatus.running,
    )
    node_run = NodeRun(
        id="nr_validate",
        run_id=run.id,
        node_id="ValidateRequest",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )
    return NodeContext(
        adapter=adapter,
        run=run,
        node_run=node_run,
        state=RunState(request=request),
    )


def test_broll_only_template_keeps_legacy_node_contracts():
    template = broll_only_template()
    output_kinds = _output_kinds_by_node(template)
    specs = {spec.node_id: spec for spec in template.nodes}

    assert template.workflow_template_id == "broll_only_v1"
    assert [spec.node_id for spec in template.nodes] == BROLL_ONLY_SEQUENCE
    assert len(template.nodes) == 13
    assert expected_node_count("broll_only_v1") == 13
    assert "LipSync" not in BROLL_ONLY_SEQUENCE
    assert template_for("broll_only_v1").workflow_template_id == "broll_only_v1"

    assert output_kinds["BrollCoveragePlanning"] == [ArtifactKind.plan_broll]
    assert output_kinds["BrollTimelinePlanning"] == [
        ArtifactKind.plan_timeline,
        ArtifactKind.plan_render,
    ]
    assert output_kinds["BrollRenderBase"] == [ArtifactKind.video_rendered]
    assert specs["BrollCoveragePlanning"].reuse_policy == "never"
    assert specs["BrollTimelinePlanning"].reuse_policy == "never"
    assert specs["BrollCoveragePlanning"].side_effects == []
    assert specs["BrollCoveragePlanning"].idempotency_key is None


def test_template_for_rejects_unknown_template_as_validation_error():
    with pytest.raises(NodeExecutionError) as exc_info:
        template_for("unknown_template")

    assert exc_info.value.error.code == ErrorCode.validation_invalid_options


def test_validate_request_applies_broll_only_policy_without_lipsync_provider():
    output = validate_request.run(
        _validate_ctx(
            DigitalHumanVideoRequest(
                case_id="case_demo",
                script="Show the repair process.",
                voice={"voice_id": "voice_sandbox"},
                workflow_template_id="broll_only_v1",
                lipsync={"enabled": True, "provider_profile_id": "missing.lipsync.profile"},
                broll={"enabled": True},
            )
        )
    )

    assert output.artifacts[0].kind == ArtifactKind.validated_production_spec
    assert output.artifacts[0].payload["workflow_template_id"] == "broll_only_v1"
