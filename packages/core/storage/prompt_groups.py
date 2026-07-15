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
#   - media_selection_agent prompt (MediaSelectionAgentPlanning) -> v2 media-only
#     portrait / B-roll ID selection.
#   - bgm_agent prompt (BgmAgentPlanning) -> BGM ID selection only.
#   - window query prompt (WindowQueryPlanning) -> per-window retrieval intent text.
# This default seed only covers the in-memory runtime path.
SEEDED_TEMPLATE_NODE_BINDINGS: dict[str, str] = {
    "prompt_cover_ai_cover": "PublishCover.ai_cover",
    "prompt_media_selection_agent": "MediaSelectionAgentPlanning",
    "prompt_bgm_agent": "BgmAgentPlanning",
    "prompt_window_query": "WindowQueryPlanning",
}

# A template may retain multiple immutable published versions. New repositories
# bind to the explicitly selected default while existing custom bindings are
# never overwritten by the in-memory seed path. Database migrations perform the
# deliberate binding switch for existing installations.
SEEDED_TEMPLATE_DEFAULT_VERSIONS: dict[str, str] = {
    "prompt_media_selection_agent": "prompt_media_selection_agent_v2",
    "prompt_bgm_agent": "prompt_bgm_agent_v1",
}


def prompt_group_seeds() -> tuple[PromptGroupSeed, ...]:
    return _load_prompt_group_seeds()


def prompt_variable_hints(template_id: str) -> list[str]:
    hints = _prompt_variable_hints_by_id().get(template_id)
    return list(hints or ())


def seed_prompt_groups(repository: Any) -> None:
    for seed in prompt_group_seeds():
        now = utcnow()
        if seed.template_id not in repository.prompt_templates:
            template = PromptTemplate(
                id=seed.template_id,
                name=seed.name,
                purpose=seed.purpose,
                variables_schema_ref=PromptSchemaRef(schema_id=seed.variables_schema_id),
                output_schema_ref=PromptSchemaRef(schema_id=seed.output_schema_id),
                status="active",
            )
            repository.prompt_templates[template.id] = template
        if seed.version_id not in repository.prompt_versions:
            version = PromptVersion(
                id=seed.version_id,
                prompt_template_id=seed.template_id,
                content=seed.content,
                status="published",
                approved_at=now,
                published_at=now,
            )
            repository.prompt_versions[version.id] = version
        node_id = SEEDED_TEMPLATE_NODE_BINDINGS.get(seed.template_id)
        if node_id is not None:
            default_version_id = SEEDED_TEMPLATE_DEFAULT_VERSIONS.get(
                seed.template_id,
                seed.version_id,
            )
            if seed.version_id != default_version_id:
                continue
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
    hints_by_template: dict[str, tuple[str, ...]] = {}
    for seed in prompt_group_seeds():
        default_version_id = SEEDED_TEMPLATE_DEFAULT_VERSIONS.get(seed.template_id)
        if default_version_id is None:
            hints_by_template.setdefault(seed.template_id, seed.variable_hints)
        elif seed.version_id == default_version_id:
            hints_by_template[seed.template_id] = seed.variable_hints
    return hints_by_template
