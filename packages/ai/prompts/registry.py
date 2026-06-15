from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from packages.core.contracts import ErrorCode, PromptBinding, PromptInvocation, PromptTemplate, PromptVersion
from packages.core.storage import Repository
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError

# Output schema ids whose contract is "must yield a non-empty口播 script string".
# Both the provider_seed CaseAgentScriptGenerate template (case_agent_script.output)
# and the migrated script-variant templates (prompt.script.output) share this rule.
SCRIPT_OUTPUT_SCHEMA_IDS = frozenset({"case_agent_script.output", "prompt.script.output"})


def _json_object(value: str) -> dict | None:
    """Parse ``value`` to a dict, or ``None`` when it is not a JSON object.

    ``None`` means "not structured JSON" (plain prose); an (even empty) dict means
    the model emitted a structured object that must be judged by the contract."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_script_from_output(output: Any) -> str:
    """Extract the口播 script text from a script-generation provider output.

    The strict contract (Spec §2.3) is JSON-items shaped: the model returns a JSON
    object whose ``items[*].script`` (or top-level ``script`` / ``draft`` /
    ``polished_script``) carries a non-empty script string. Real LLM plugins surface
    the model reply under ``content`` (a string), so we also accept ``content``:

      * If ``content`` parses to a JSON object that follows the structured contract
        (it has ``items`` or a script field) we ONLY trust its nested script; a
        structured-but-script-less object is a FAILED reply and yields ``""`` (it is
        NOT silently treated as a script). This is the no-silent-degrade guard:
        ``{"items": [{"title": "x"}]}`` must not pass as a usable script.
      * Otherwise (plain-prose ``content``) the model wrote the script directly as
        text, so the trimmed content IS the script.

    Returns ``""`` when no usable script exists; callers map that to
    ``ErrorCode.prompt_output_invalid``.
    """
    if not isinstance(output, dict):
        return ""
    nested = _script_from_items(output.get("items"))
    if nested:
        return nested
    for key in ("script", "draft", "polished_script"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    content = output.get("content")
    if isinstance(content, str) and content.strip():
        return _script_from_content(content)
    return ""


def _script_from_items(items: Any) -> str:
    if isinstance(items, list) and items and isinstance(items[0], dict):
        for nested_key in ("script", "content", "draft"):
            nested = items[0].get(nested_key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _script_from_content(content: str) -> str:
    parsed = _json_object(content)
    if parsed is None:
        # content is plain prose (not a JSON object) -> it IS the script.
        return content.strip()
    # content is a structured JSON object (possibly empty): only its nested script
    # counts. A structured object missing a usable script is a failed reply -> "".
    nested = _script_from_items(parsed.get("items"))
    if nested:
        return nested
    for nested_key in ("script", "draft", "polished_script"):
        value = parsed.get(nested_key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def case_prompt_variables(case: Any) -> dict[str, str]:
    """Bridge a Case contract object to the prompt-template variable vocabulary.

    The Case contract keeps its own field names (name, product, strategy_tags, ...),
    while the prompt templates in prompt_group_defaults.json reference a different
    vocabulary ({case_name}{product_name}{industry}{target_audience}{ip_persona}
    {brand_voice}{key_selling_points}{description}{tags}). This helper maps between
    them so #B wiring fills the templates with real Case values instead of leaving
    them permanently empty.

    List fields are serialized as ", ".join(...) so registry.render's str()-based
    token replacement does not emit a Python list repr. brand_keywords and
    competitor_names are intentionally NOT mapped: no template var references them
    today (they are stored/returned but not yet wired into render).
    """

    def _text(value: Any) -> str:
        return "" if value is None else str(value)

    def _joined(value: Any) -> str:
        if not value:
            return ""
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        return str(value)

    return {
        "case_name": _text(getattr(case, "name", None)),
        "product_name": _text(getattr(case, "product", None)),
        "industry": _text(getattr(case, "industry", None)),
        "target_audience": _text(getattr(case, "target_audience", None)),
        "ip_persona": _text(getattr(case, "ip_persona", None)),
        "brand_voice": _text(getattr(case, "brand_voice", None)),
        "key_selling_points": _joined(getattr(case, "key_selling_points", None)),
        "description": _text(getattr(case, "description", None)),
        "tags": _joined(getattr(case, "strategy_tags", None)),
    }


class PromptRuntimeReader(Protocol):
    def resolve_published_version(
        self,
        *,
        node_id: str,
        case_id: str | None = None,
        provider_profile_id: str | None = None,
    ) -> tuple[PromptBinding, PromptVersion]:
        ...

    def get_template_for_version(self, prompt_version_id: str) -> PromptTemplate:
        ...


@dataclass
class PromptRegistry:
    repository: Repository
    prompt_reader: PromptRuntimeReader | None = None

    def resolve_published_version(
        self,
        *,
        node_id: str,
        case_id: str | None = None,
        provider_profile_id: str | None = None,
    ):
        if self.prompt_reader is not None:
            return self.prompt_reader.resolve_published_version(
                node_id=node_id,
                case_id=case_id,
                provider_profile_id=provider_profile_id,
            )
        candidates = [
            binding
            for binding in self.repository.prompt_bindings.values()
            if binding.enabled
            and (binding.node_id is None or binding.node_id == node_id)
            and (binding.case_id is None or binding.case_id == case_id)
            and (
                binding.provider_profile_id is None
                or binding.provider_profile_id == provider_profile_id
            )
        ]
        candidates.sort(key=lambda item: item.priority)
        for binding in candidates:
            version = self.repository.prompt_versions[binding.prompt_version_id]
            if version.status == "published":
                return binding, version
        raise NodeExecutionError(
            ErrorCode.prompt_version_not_published,
            f"No published prompt version is bound to {node_id}.",
        )

    def render(
        self,
        *,
        node_id: str,
        variables: dict,
        case_id: str | None = None,
        run_id: str | None = None,
        node_run_id: str | None = None,
        provider_profile_id: str | None = None,
    ) -> tuple[PromptInvocation, str]:
        binding, version = self.resolve_published_version(
            node_id=node_id,
            case_id=case_id,
            provider_profile_id=provider_profile_id,
        )
        missing = [
            token.split("}", 1)[0]
            for token in version.content.split("{")
            if "}" in token and token.split("}", 1)[0] not in variables
        ]
        if missing:
            raise NodeExecutionError(
                ErrorCode.prompt_render_error,
                f"Missing prompt variables: {', '.join(sorted(missing))}",
                details={"missing": missing},
            )
        rendered = version.content
        for key, value in variables.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        invocation = PromptInvocation(
            id=new_id("prinv"),
            prompt_template_id=binding.prompt_template_id,
            prompt_version_id=version.id,
            case_id=case_id,
            run_id=run_id,
            node_run_id=node_run_id,
            variables_artifact_id=None,
        )
        self.repository.prompt_invocations[invocation.id] = invocation
        return invocation, rendered

    def validate_output(self, *, prompt_version_id: str, output: dict) -> None:
        if self.prompt_reader is not None:
            template = self.prompt_reader.get_template_for_version(prompt_version_id)
        else:
            version = self.repository.prompt_versions[prompt_version_id]
            template = self.repository.prompt_templates[version.prompt_template_id]
        schema_id = template.output_schema_ref.schema_id
        if schema_id == "creative_intent.output":
            intent = output.get("intent")
            if not isinstance(intent, dict):
                raise NodeExecutionError(
                    ErrorCode.prompt_output_invalid,
                    "Creative intent output must contain an intent object.",
                )
            if not isinstance(intent.get("hook"), str) or not isinstance(intent.get("beats"), list):
                raise NodeExecutionError(
                    ErrorCode.prompt_output_invalid,
                    "Creative intent output is missing hook or beats.",
                )
        elif schema_id in SCRIPT_OUTPUT_SCHEMA_IDS:
            # Spec §2.3: a non-empty-but-malformed reply that yields no usable
            # script must NOT pass silently -> prompt.output_invalid (the caller
            # retries up to the bound, then hard_fails on exhaustion).
            if not isinstance(output, dict):
                raise NodeExecutionError(
                    ErrorCode.prompt_output_invalid,
                    f"Script prompt output for schema {schema_id} must be a JSON object.",
                )
            if not extract_script_from_output(output):
                raise NodeExecutionError(
                    ErrorCode.prompt_output_invalid,
                    "Script prompt output is missing a non-empty script field.",
                )
        elif schema_id == "publish_copy.output":
            # §2.3 hard-fail: the Publishing Copy Node output must carry the four
            # string fields (title / publish_content / cover_title / cover_subtitle).
            from packages.publishing.copy_node import validate_publish_copy_output

            validate_publish_copy_output(output)
        elif not isinstance(output, dict):
            raise NodeExecutionError(
                ErrorCode.prompt_output_invalid,
                f"Prompt output for schema {schema_id} must be an object.",
            )
