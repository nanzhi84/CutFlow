"""Wiring that backs the Publishing Copy Node with a real ``llm.chat`` provider.

``copy_node.py`` stays provider-agnostic (it takes an injected ``LlmChatPort``).
This module builds that port from a ``ProviderGateway`` + the seeded
``PublishingCopy`` prompt, selecting an enabled real ``llm.chat`` ProviderProfile
with an active secret. When no real LLM is armed it returns ``None`` so the copy
node falls back to its deterministic, non-fabricated derivation.

Shared by both the production ``ExportFinishedVideo`` node (title + cover copy)
and the publish-center copy endpoints, so neither re-implements the wiring.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

from packages.ai.gateway import ProviderCall
from packages.ai.prompts.registry import PromptRegistry
from packages.core.contracts import ErrorCode
from packages.core.provider_idempotency import ProviderCallIdempotency
from packages.core.workflow import NodeExecutionError
from packages.publishing.copy_node import LlmChatPort, PublishCopyContext

PUBLISHING_COPY_NODE_ID = "PublishingCopy"
_PUBLISH_COPY_FIELDS = ("title", "publish_content", "cover_title", "cover_subtitle")


def _select_real_llm_profile(gateway, repository):
    """Return an enabled real ``llm.chat`` ProviderProfile with an active secret,
    or ``None``. Mirrors the gating used elsewhere (case agent / cover): sandbox
    profiles, unregistered plugins, and inactive secrets are all excluded."""
    for profile in repository.provider_profiles.values():
        if profile.capability != "llm.chat" or not profile.enabled:
            continue
        if profile.provider_id == "sandbox":
            continue
        if profile.provider_id not in gateway.plugins:
            continue
        if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
            continue
        return profile
    return None


def build_copy_llm_chat(
    *,
    gateway,
    repository,
    prompt_registry: PromptRegistry | None = None,
    case_id: str | None = None,
    run_id: str | None = None,
    node_run_id: str | None = None,
    idempotency_for_profile: Callable[[str], ProviderCallIdempotency] | None = None,
) -> LlmChatPort | None:
    """Build an ``LlmChatPort`` for the Publishing Copy Node, or ``None`` when no
    real ``llm.chat`` provider is armed (caller then uses deterministic copy).

    ``idempotency_for_profile`` takes the selected profile id and returns the durable
    identity for this call — a Workflow node passes its Job-scoped one so a re-run
    replays the copy instead of buying it again. The publish-center endpoints run
    outside a Run and pass nothing: their copy call stays transient.
    """
    profile = _select_real_llm_profile(gateway, repository)
    if profile is None:
        return None
    idempotency = (
        idempotency_for_profile(profile.id) if idempotency_for_profile is not None else None
    )
    registry = prompt_registry or PromptRegistry(repository)

    def _llm_chat(*, context: PublishCopyContext) -> tuple[dict, str | None]:
        variables = {
            "case_name": context.case_name or "未指定",
            "description": context.description or "",
            "script": context.script or "",
        }
        prompt_invocation, rendered = registry.render(
            node_id=PUBLISHING_COPY_NODE_ID,
            variables=variables,
            case_id=case_id,
            run_id=run_id,
            node_run_id=node_run_id,
            provider_profile_id=profile.id,
        )
        invocation, result = gateway.invoke(
            ProviderCall(
                case_id=case_id,
                run_id=run_id,
                node_run_id=node_run_id,
                provider_profile_id=profile.id,
                capability_id="llm.chat",
                prompt_version_id=prompt_invocation.prompt_version_id,
                input={"prompt": rendered},
                idempotency_key=idempotency.key if idempotency is not None else None,
                fallback_idempotency_keys=(
                    list(idempotency.fallback_keys) if idempotency is not None else []
                ),
            )
        )
        repository.prompt_invocations[prompt_invocation.id] = prompt_invocation.model_copy(
            update={
                "provider_invocation_id": invocation.id,
                "status": "failed" if invocation.error else "succeeded",
            }
        )
        if result is None or invocation.error:
            raise NodeExecutionError(
                invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                invocation.error.message
                if invocation.error
                else "Publishing copy LLM provider failed.",
            )
        return _extract_publish_copy_payload(result.output), invocation.id

    return _llm_chat


def _extract_publish_copy_payload(output: Any) -> dict:
    """Normalize generic chat provider output to the publish-copy contract shape.

    Real chat providers surface model text as ``{"content": "...", "intent": {...}}``.
    The PublishingCopy node validates the parsed JSON object itself, so unwrap the
    generic envelope here and let ``copy_node`` remain provider-agnostic.
    """
    if not isinstance(output, dict):
        raise NodeExecutionError(
            ErrorCode.prompt_output_invalid,
            "Publish copy output must be a JSON object.",
        )
    if _has_publish_copy_fields(output):
        return output
    intent = output.get("intent")
    if isinstance(intent, dict) and _has_publish_copy_fields(intent):
        return intent
    content = output.get("content")
    if isinstance(content, str) and content.strip():
        parsed = _parse_json_object(content)
        if parsed is not None:
            return parsed
    if isinstance(intent, dict):
        return intent
    return output


def _has_publish_copy_fields(value: dict) -> bool:
    return all(isinstance(value.get(field), str) for field in _PUBLISH_COPY_FIELDS)


def _parse_json_object(content: str) -> dict | None:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        fence = lines[0].strip()
        if fence.startswith("```") and fence[3:].strip().lower() in {"", "json"}:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
