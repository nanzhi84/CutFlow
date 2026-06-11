from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.core.contracts import ErrorCode, PromptBinding, PromptInvocation, PromptTemplate, PromptVersion
from packages.core.storage import Repository
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError


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
        elif not isinstance(output, dict):
            raise NodeExecutionError(
                ErrorCode.prompt_output_invalid,
                f"Prompt output for schema {schema_id} must be an object.",
            )
