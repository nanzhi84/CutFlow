from __future__ import annotations

import json

from fastapi import Request

from apps.api.common import repository
from packages.ai.gateway import ProviderCall
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError


def generate_script_with_llm(
    case_id: str,
    brief: str,
    memory_ids: list[str],
    memories: list[str],
    request: Request,
) -> str | None:
    profile = _select_real_llm_profile(request)
    if profile is None:
        return None
    prompt_invocation, rendered = request.app.state.prompt_registry.render(
        node_id="CaseAgentScriptGenerate",
        variables={"brief": brief, "memories": " / ".join(memories) if memories else "暂无"},
        case_id=case_id,
        provider_profile_id=profile.id,
    )
    invocation, result = request.app.state.provider_gateway.invoke(
        ProviderCall(
            case_id=case_id,
            provider_profile_id=profile.id,
            capability_id="llm.chat",
            prompt_version_id=prompt_invocation.prompt_version_id,
            input={"prompt": rendered, "brief": brief, "memory_ids": memory_ids, "memories": memories},
        )
    )
    if result is None or invocation.error:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else c.ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "Case agent LLM provider failed.",
        )
    repository(request).prompt_invocations[prompt_invocation.id] = prompt_invocation.model_copy(
        update={"provider_invocation_id": invocation.id, "updated_at": c.utcnow()}
    )
    script = _script_from_llm_output(result.output)
    if not script:
        raise NodeExecutionError(c.ErrorCode.provider_remote_failed, "Case agent LLM output missing script.")
    return script


def _select_real_llm_profile(request: Request) -> c.ProviderProfile | None:
    gateway = request.app.state.provider_gateway
    for profile in repository(request).provider_profiles.values():
        if profile.capability != "llm.chat" or not profile.enabled or profile.provider_id == "sandbox":
            continue
        if profile.provider_id not in gateway.plugins:
            continue
        if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
            continue
        return profile
    return None


def _script_from_llm_output(output: dict) -> str:
    for key in ("script", "draft", "polished_script", "content"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            if key == "content":
                parsed = _json_object(value)
                for nested_key in ("script", "draft", "polished_script"):
                    nested = parsed.get(nested_key)
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()
            return value.strip()
    return ""


def _json_object(value: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
