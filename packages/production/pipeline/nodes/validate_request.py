"""ValidateRequest node: gate the request and emit the validated spec."""

from __future__ import annotations

from packages.core.contracts import (
    ArtifactKind,
    ErrorCode,
    ValidatedProductionSpec,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    request = ctx.state.request
    repository = ctx.repository
    node_ids = {spec.node_id for spec in ctx.template.nodes}
    if request.case_id not in repository.cases:
        raise NodeExecutionError(ErrorCode.validation_missing_case, "Case does not exist.")
    if not request.script.strip():
        raise NodeExecutionError(ErrorCode.validation_missing_script, "Script is required.")
    voice_id = request.voice.voice_id or "voice_sandbox"
    if voice_id not in repository.voices or not repository.voices[voice_id].enabled:
        raise NodeExecutionError(ErrorCode.validation_missing_voice, "Voice is missing or disabled.")
    if request.lipsync.enabled and "LipSync" in node_ids:
        profile = repository.provider_profiles.get(request.lipsync.provider_profile_id)
        if profile is None or profile.capability != "lipsync.video":
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "LipSync provider profile is missing or incompatible.",
            )
    if "BrollCoveragePlanning" in node_ids and not request.broll.enabled:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "B_roll must be enabled in B_roll-only mode.",
        )
    artifact = ctx.artifact(
        ArtifactKind.validated_production_spec,
        ValidatedProductionSpec(
            request=request,
            workflow_template_id=ctx.template.workflow_template_id,
            workflow_version=ctx.template.version,
        ).model_dump(mode="json"),
        "ValidatedProductionSpec.v1",
    )
    return NodeOutput(artifacts=[artifact])
