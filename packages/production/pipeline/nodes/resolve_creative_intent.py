from __future__ import annotations

from packages.ai.gateway import ProviderCall
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.contracts import ArtifactKind, ErrorCode, NodeStatus, normalize_bgm_mood, utcnow
from packages.core.contracts.artifacts import CreativeIntentArtifact, EmphasisHint
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext

# An emphasis hint is a short key phrase (a quote from the script) the LLM marks as
# worth a 花字-style emphasis; bound the count/length so a hallucinated paragraph or
# flood of phrases can't bloat the plan.
_MAX_EMPHASIS = 6
_MIN_PHRASE_LEN = 2
_MAX_PHRASE_LEN = 30


def _clean_phrase(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not (_MIN_PHRASE_LEN <= len(text) <= _MAX_PHRASE_LEN):
        return None
    return text


def _intent_to_artifact(output: dict) -> CreativeIntentArtifact:
    """Map the LLM output into a typed CreativeIntentArtifact.

    The LLM emits {hook, beats, emphasis, ...} which the provider wraps under the
    ``intent`` object (validate_output requires intent.hook/beats). We promote the
    emphasis phrases to a typed field, dropping malformed/duplicate entries so
    downstream consumers never see junk; the raw intent blob is preserved as-is.
    """
    intent = output.get("intent") if isinstance(output.get("intent"), dict) else {}
    normalized_bgm_mood = normalize_bgm_mood(intent.get("bgm_mood")) if intent else ""
    if normalized_bgm_mood:
        intent = {**intent, "bgm_mood": normalized_bgm_mood}
    emphasis: list[EmphasisHint] = []
    seen: set[str] = set()
    raw_emphasis = intent.get("emphasis")
    if isinstance(raw_emphasis, list):
        for item in raw_emphasis:
            phrase = _clean_phrase(item)
            if phrase is None or phrase in seen:
                continue
            seen.add(phrase)
            emphasis.append(EmphasisHint(phrase=phrase))
            if len(emphasis) >= _MAX_EMPHASIS:
                break
    return CreativeIntentArtifact(intent=intent or None, emphasis=emphasis)


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
        if not sandbox_fallback_allowed():
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "未配置可用的真实 LLM 供应商（llm.chat）。请在「设置」中配置并启用真实 LLM 供应商及密钥。",
            )
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
            idempotency_key=f"{run.id}:{node_run.id}:resolve_creative_intent",
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
        _intent_to_artifact(result.output).model_dump(mode="json"),
        "CreativeIntentArtifact.v1",
    )
    return NodeOutput(artifacts=[artifact], provider_invocation_ids=[invocation.id])
