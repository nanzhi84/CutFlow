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
    digital_human_template,
    template_for,
)
from packages.production.pipeline.nodes import validate_request
from packages.production.pipeline.node_sequence import (
    BROLL_ONLY_SEQUENCE,
    NODE_SEQUENCE,
    expected_node_count,
)


def _output_kinds_by_node(template):
    return {spec.node_id: list(spec.output_artifact_kinds) for spec in template.nodes}


def _validate_ctx(request: DigitalHumanVideoRequest) -> NodeContext:
    repository = Repository()
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
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


def test_broll_only_template_registers_thirteen_node_sequence():
    template = broll_only_template()

    assert template.workflow_template_id == "broll_only_v1"
    assert template.version == "v1"
    assert [spec.node_id for spec in template.nodes] == BROLL_ONLY_SEQUENCE
    assert len(template.nodes) == 13
    assert "PortraitPlanning" not in BROLL_ONLY_SEQUENCE
    assert "PortraitTrackBuild" not in BROLL_ONLY_SEQUENCE
    assert "LipSync" not in BROLL_ONLY_SEQUENCE
    assert expected_node_count("broll_only_v1") == 13


def test_template_for_dispatches_known_templates_and_rejects_unknown_id():
    assert template_for("broll_only_v1").workflow_template_id == "broll_only_v1"
    assert template_for("digital_human_v2").workflow_template_id == "digital_human_v2"

    # workflow_template_id is a free-form request string; an unknown id must surface
    # as a NodeExecutionError so the API maps it to a clean 4xx ErrorEnvelope rather
    # than letting a bare ValueError bubble into an uncaught 500 at job admission.
    with pytest.raises(NodeExecutionError) as exc_info:
        template_for("unknown_template")
    assert exc_info.value.error.code == ErrorCode.validation_invalid_options


def test_digital_human_template_keeps_existing_sequence_edges_and_outputs():
    template = digital_human_template()

    assert template.workflow_template_id == "digital_human_v2"
    assert [spec.node_id for spec in template.nodes] == NODE_SEQUENCE
    assert len(template.nodes) == 18
    assert len(template.edges) == len(NODE_SEQUENCE) - 1
    assert [
        (edge.from_node_id, edge.to_node_id)
        for edge in template.edges
    ] == list(zip(NODE_SEQUENCE, NODE_SEQUENCE[1:]))
    assert _output_kinds_by_node(template) == {
        "ValidateRequest": [ArtifactKind.validated_production_spec],
        "LoadCaseContext": [ArtifactKind.case_context],
        "ResolveCreativeIntent": [ArtifactKind.creative_intent],
        "TTS": [ArtifactKind.audio_tts],
        "MaterialPackPlanning": [ArtifactKind.plan_material_pack],
        "NarrationAlignment": [ArtifactKind.audio_alignment, ArtifactKind.narration_units],
        "NarrationBoundaryPlanning": [ArtifactKind.plan_narration_boundary],
        "TimelineWindowPlanning": [ArtifactKind.plan_timeline_windows],
        "PortraitPlanning": [ArtifactKind.plan_portrait],
        "BrollPlanning": [ArtifactKind.plan_broll],
        "StylePlanning": [ArtifactKind.plan_style],
        "TimelinePlanning": [ArtifactKind.plan_timeline, ArtifactKind.plan_render],
        "PortraitTrackBuild": [ArtifactKind.video_portrait_track],
        "LipSync": [ArtifactKind.video_lipsync, ArtifactKind.lipsync_report],
        "RenderFinalTimeline": [ArtifactKind.video_rendered],
        "SubtitleAndBgmMix": [ArtifactKind.video_final, ArtifactKind.subtitle_ass],
        "ExportFinishedVideo": [
            ArtifactKind.video_finished,
            ArtifactKind.cover_image,
            ArtifactKind.publish_package,
        ],
        "FinalizeRunReport": [ArtifactKind.run_report_public, ArtifactKind.run_report_debug],
    }
    specs = {spec.node_id: spec for spec in template.nodes}
    assert specs["NarrationBoundaryPlanning"].reuse_policy == "never"
    assert specs["TimelineWindowPlanning"].reuse_policy == "never"
    assert specs["PortraitPlanning"].reuse_policy == "never"
    assert specs["BrollPlanning"].reuse_policy == "never"
    assert specs["TimelinePlanning"].reuse_policy == "never"


def test_material_pack_planning_retries_retryable_reservation_conflicts():
    for template in (digital_human_template(), broll_only_template()):
        specs = {spec.node_id: spec for spec in template.nodes}
        assert specs["MaterialPackPlanning"].retry_policy.max_attempts == 3
        assert specs["MaterialPackPlanning"].retry_policy.backoff_seconds == 1
        for node_id, spec in specs.items():
            if node_id != "MaterialPackPlanning":
                assert spec.retry_policy.max_attempts == 1


def test_broll_only_template_declares_new_node_outputs_and_provider_side_effects():
    template = broll_only_template()
    output_kinds = _output_kinds_by_node(template)

    assert output_kinds["BrollCoveragePlanning"] == [ArtifactKind.plan_broll]
    assert output_kinds["BrollTimelinePlanning"] == [
        ArtifactKind.plan_timeline,
        ArtifactKind.plan_render,
    ]
    assert output_kinds["BrollRenderBase"] == [ArtifactKind.video_rendered]

    specs = {spec.node_id: spec for spec in template.nodes}
    assert specs["TTS"].side_effects == ["provider_call"]
    assert specs["TTS"].idempotency_key == "broll_only_v1:TTS:{input_manifest_hash}"
    assert specs["ResolveCreativeIntent"].side_effects == ["provider_call"]
    assert specs["BrollCoveragePlanning"].side_effects == []
    assert specs["BrollCoveragePlanning"].idempotency_key is None
    assert specs["BrollCoveragePlanning"].reuse_policy == "never"
    assert specs["BrollTimelinePlanning"].reuse_policy == "never"


def test_validate_request_skips_lipsync_provider_when_template_has_no_lipsync_node():
    ctx = _validate_ctx(
        DigitalHumanVideoRequest(
            case_id="case_demo",
            script="施工过程展示补漆修复。",
            voice={"voice_id": "voice_sandbox"},
            workflow_template_id="broll_only_v1",
            lipsync={"enabled": True, "provider_profile_id": "missing.lipsync.profile"},
            broll={"enabled": True},
        )
    )

    output = validate_request.run(ctx)

    assert output.artifacts[0].kind == ArtifactKind.validated_production_spec
    assert output.artifacts[0].payload["workflow_template_id"] == "broll_only_v1"


def test_validate_request_requires_broll_when_template_has_coverage_node():
    ctx = _validate_ctx(
        DigitalHumanVideoRequest(
            case_id="case_demo",
            script="施工过程展示补漆修复。",
            voice={"voice_id": "voice_sandbox"},
            workflow_template_id="broll_only_v1",
            broll={"enabled": False},
        )
    )

    with pytest.raises(NodeExecutionError) as exc:
        validate_request.run(ctx)

    assert exc.value.error.code == ErrorCode.validation_invalid_options
