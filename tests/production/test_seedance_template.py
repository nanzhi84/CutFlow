from __future__ import annotations

from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.repository import Repository
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import (
    LocalRuntimeAdapter,
    seedance_t2v_template,
    template_for,
)
from packages.production.pipeline.node_sequence import SEEDANCE_T2V_SEQUENCE, expected_node_count
from packages.production.pipeline.nodes import validate_request


def _output_kinds_by_node(template):
    return {spec.node_id: list(spec.output_artifact_kinds) for spec in template.nodes}


def test_seedance_template_registers_five_node_sequence():
    template = seedance_t2v_template()
    assert template.workflow_template_id == "seedance_t2v_v1"
    assert template.version == "v1"
    assert [spec.node_id for spec in template.nodes] == SEEDANCE_T2V_SEQUENCE
    assert SEEDANCE_T2V_SEQUENCE == [
        "ValidateRequest",
        "LoadCaseContext",
        "SeedanceGenerateVideo",
        "ExportSeedanceVideo",
        "FinalizeRunReport",
    ]
    assert expected_node_count("seedance_t2v_v1") == 5
    # No TTS / portrait / lipsync nodes in the seedance chain.
    assert "TTS" not in SEEDANCE_T2V_SEQUENCE
    assert "LipSync" not in SEEDANCE_T2V_SEQUENCE
    assert "PortraitPlanning" not in SEEDANCE_T2V_SEQUENCE


def test_template_for_dispatches_seedance():
    assert template_for("seedance_t2v_v1").workflow_template_id == "seedance_t2v_v1"


def test_seedance_node_outputs_and_provider_side_effect():
    template = seedance_t2v_template()
    output_kinds = _output_kinds_by_node(template)
    assert output_kinds["SeedanceGenerateVideo"] == [ArtifactKind.video_rendered]
    assert output_kinds["ExportSeedanceVideo"] == [
        ArtifactKind.video_finished,
        ArtifactKind.cover_image,
        ArtifactKind.publish_package,
    ]
    specs = {spec.node_id: spec for spec in template.nodes}
    # The paid Seedance call must be a declared side effect with a non-None
    # idempotency_key so resume never silently re-bills a generation.
    assert specs["SeedanceGenerateVideo"].side_effects == ["provider_call"]
    assert specs["SeedanceGenerateVideo"].idempotency_key is not None
    # The export node is pure assembly.
    assert specs["ExportSeedanceVideo"].side_effects == []


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
    return NodeContext(adapter=adapter, run=run, node_run=node_run, state=RunState(request=request))


def test_validate_request_skips_voice_for_seedance_template():
    # A non-empty voice_id that does NOT exist would fail validation for a TTS
    # template; the seedance chain has no TTS node, so it must pass.
    ctx = _validate_ctx(
        DigitalHumanVideoRequest(
            case_id="case_demo",
            script="门头特写，暖光，产品摆放整齐。",
            voice={"voice_id": "voice_does_not_exist"},
            workflow_template_id="seedance_t2v_v1",
        )
    )
    output = validate_request.run(ctx)
    assert output.artifacts[0].kind == ArtifactKind.validated_production_spec
    assert output.artifacts[0].payload["workflow_template_id"] == "seedance_t2v_v1"
