"""ValidateRequest node: gate the request and emit the validated spec."""

from __future__ import annotations

from packages.core.contracts import (
    ArtifactKind,
    ErrorCode,
    ValidatedProductionSpec,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._broll_policy import broll_full_coverage_enabled


def run(ctx: NodeContext) -> NodeOutput:
    request = ctx.state.request
    repository = ctx.repository
    node_ids = {spec.node_id for spec in ctx.template.nodes}
    if request.case_id not in repository.cases:
        raise NodeExecutionError(ErrorCode.validation_missing_case, "Case does not exist.")
    if not request.script.strip():
        raise NodeExecutionError(ErrorCode.validation_missing_script, "Script is required.")
    # Voice is only required for templates that actually synthesize speech (TTS).
    # Seedance (text/image-to-video) has no TTS node, so it must not be gated on a
    # voice it never uses.
    if "TTS" in node_ids:
        voice_id = request.voice.voice_id or "voice_sandbox"
        if voice_id not in repository.voices or not repository.voices[voice_id].enabled:
            raise NodeExecutionError(
                ErrorCode.validation_missing_voice, "Voice is missing or disabled."
            )
    if (
        request.lipsync.enabled
        and "LipSync" in node_ids
        and not broll_full_coverage_enabled(request)
    ):
        profile = repository.provider_profiles.get(request.lipsync.provider_profile_id)
        if profile is None or profile.capability != "lipsync.video":
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "LipSync provider profile is missing or incompatible.",
            )
    if request.broll.mode == "full_coverage" and not request.broll.enabled:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "B-roll must be enabled when broll.mode is full_coverage.",
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
