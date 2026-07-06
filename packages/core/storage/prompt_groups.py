from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from packages.core.contracts import (
    PromptBinding,
    PromptSchemaRef,
    PromptTemplate,
    PromptVersion,
    utcnow,
)


@dataclass(frozen=True)
class PromptGroupSeed:
    template_id: str
    version_id: str
    name: str
    purpose: str
    variables_schema_id: str
    output_schema_id: str
    variable_hints: tuple[str, ...]
    content: str


# Default node bindings for seeded prompt-group templates that have a runtime
# consumer in THIS codebase. Spec §10.1: production prompts must resolve through
# the registry (via a binding), not be looked up/hardcoded in node code. Only seed
# a binding for a template a node actually renders today, so we never imply
# coverage that does not exist:
#   - ai_cover_prompt (PublishCover.ai_cover) -> ExportFinishedVideo AI cover node.
#   - editing_agent prompt (EditingAgentPlanning) -> digital_human_editing_agent_v1
#     LLM综合剪辑 node (issue #136).
#   - window query prompt (WindowQueryPlanning) -> per-window retrieval intent text.
# This default seed only covers the in-memory runtime path.
SEEDED_TEMPLATE_NODE_BINDINGS: dict[str, str] = {
    "prompt_cover_ai_cover": "PublishCover.ai_cover",
    "prompt_editing_agent": "EditingAgentPlanning",
    "prompt_window_query": "WindowQueryPlanning",
}


def prompt_group_seeds() -> tuple[PromptGroupSeed, ...]:
    return _load_prompt_group_seeds()


def prompt_variable_hints(template_id: str) -> list[str]:
    hints = _prompt_variable_hints_by_id().get(template_id)
    return list(hints or ())


def seed_prompt_groups(repository: Any) -> None:
    for seed in prompt_group_seeds():
        if seed.template_id in repository.prompt_templates:
            continue
        now = utcnow()
        template = PromptTemplate(
            id=seed.template_id,
            name=seed.name,
            purpose=seed.purpose,
            variables_schema_ref=PromptSchemaRef(schema_id=seed.variables_schema_id),
            output_schema_ref=PromptSchemaRef(schema_id=seed.output_schema_id),
            status="active",
        )
        version = PromptVersion(
            id=seed.version_id,
            prompt_template_id=seed.template_id,
            content=seed.content,
            status="published",
            approved_at=now,
            published_at=now,
        )
        repository.prompt_templates[template.id] = template
        repository.prompt_versions[version.id] = version
        node_id = SEEDED_TEMPLATE_NODE_BINDINGS.get(seed.template_id)
        if node_id is not None:
            binding_id = f"prompt_binding_{seed.template_id}"
            if binding_id not in repository.prompt_bindings:
                repository.prompt_bindings[binding_id] = PromptBinding(
                    id=binding_id,
                    prompt_template_id=seed.template_id,
                    prompt_version_id=seed.version_id,
                    node_id=node_id,
                    priority=1,
                )


@lru_cache(maxsize=1)
def _load_prompt_group_seeds() -> tuple[PromptGroupSeed, ...]:
    path = Path(__file__).with_name("prompt_group_defaults.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        PromptGroupSeed(
            template_id=item["template_id"],
            version_id=item["version_id"],
            name=item["name"],
            purpose=item["purpose"],
            variables_schema_id=item["variables_schema_id"],
            output_schema_id=item["output_schema_id"],
            variable_hints=tuple(item["variable_hints"]),
            content=item["content"],
        )
        for item in payload["items"]
    )


@lru_cache(maxsize=1)
def _prompt_variable_hints_by_id() -> dict[str, tuple[str, ...]]:
    return {seed.template_id: seed.variable_hints for seed in prompt_group_seeds()}
