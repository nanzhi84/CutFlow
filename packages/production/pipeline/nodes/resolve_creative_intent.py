"""ResolveCreativeIntent node: derive creative intent via an LLM provider."""

from __future__ import annotations

from packages.ai.gateway import ProviderCall
from packages.core.contracts import ArtifactKind, ErrorCode, NodeStatus, utcnow
from packages.core.contracts.artifacts import CreativeIntentArtifact
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    if state.request.creative_intent_ref:
        existing = ctx.repository.artifacts.get(state.request.creative_intent_ref.artifact_id)
        if existing is None:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Creative intent artifact missing.")
        return NodeOutput(artifacts=[existing], status=NodeStatus.skipped)
    profile = ctx.first_available_provider_profile("llm.chat", include_sandbox=False)
    if profile is None:
        profile = ctx.repository.provider_profiles["sandbox.llm.default"]
    prompt_invocation, rendered = ctx.prompt_registry.render(
        node_id="ResolveCreativeIntent",
        variables={"script": state.request.script},
        case_id=run.case_id,
        run_id=run.id,
        node_run_id=node_run.id,
        provider_profile_id=profile.id,
    )
    invocation, result = ctx.provider_gateway.invoke(
        ProviderCall(
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=profile.id,
            capability_id="llm.chat",
            prompt_version_id=prompt_invocation.prompt_version_id,
            input={"prompt": rendered, "script": state.request.script},
        )
    )
    if result is None or invocation.error:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "Provider failed.",
            retryable=True,
        )
    ctx.prompt_registry.validate_output(
        prompt_version_id=prompt_invocation.prompt_version_id,
        output=result.output,
    )
    prompt_invocation = prompt_invocation.model_copy(
        update={"provider_invocation_id": invocation.id, "updated_at": utcnow()}
    )
    ctx.repository.prompt_invocations[prompt_invocation.id] = prompt_invocation
    artifact = ctx.artifact(
        ArtifactKind.creative_intent,
        CreativeIntentArtifact(intent=result.output.get("intent")).model_dump(mode="json"),
        "CreativeIntentArtifact.v1",
    )
    return NodeOutput(artifacts=[artifact], provider_invocation_ids=[invocation.id])
