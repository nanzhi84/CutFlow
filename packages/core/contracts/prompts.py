"""Prompts domain: templates, versions, bindings, and experiments."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import Field

from .base import BaseListQuery, ContractModel, EntityMeta


class PromptSchemaRef(ContractModel):
    schema_id: str
    schema_version: str = "v1"


class PromptTemplate(EntityMeta):
    name: str
    purpose: str
    variables_schema_ref: PromptSchemaRef
    output_schema_ref: PromptSchemaRef
    status: Literal["draft", "active", "deprecated"] = "draft"


class PromptVersion(EntityMeta):
    prompt_template_id: str
    content: str
    status: Literal["draft", "reviewing", "approved", "published", "deprecated", "rolled_back"] = (
        "draft"
    )
    changelog: str | None = None
    approved_at: datetime | None = None
    published_at: datetime | None = None


class PromptBinding(EntityMeta):
    prompt_template_id: str
    prompt_version_id: str
    case_id: str | None = None
    node_id: str | None = None
    provider_profile_id: str | None = None
    priority: int = 100
    enabled: bool = True


class PromptInvocation(EntityMeta):
    prompt_template_id: str
    prompt_version_id: str
    case_id: str | None = None
    run_id: str | None = None
    node_run_id: str | None = None
    provider_invocation_id: str | None = None
    variables_artifact_id: str | None = None
    output_artifact_id: str | None = None
    status: Literal["succeeded", "failed"] = "succeeded"


class PromptTemplateQuery(BaseListQuery):
    status: str | None = None
    purpose: str | None = None


class PromptTemplateView(ContractModel):
    template: PromptTemplate
    published_version: PromptVersion | None = None
    variable_hints: list[str] = Field(default_factory=list)


class PromptVersionView(ContractModel):
    version: PromptVersion
    template: PromptTemplate | None = None


class PromptBindingQuery(BaseListQuery):
    case_id: str | None = None
    node_id: str | None = None


class PromptBindingView(ContractModel):
    binding: PromptBinding
    resolved_version: PromptVersion | None = None


class CreatePromptTemplateRequest(ContractModel):
    name: str
    purpose: str
    variables_schema_ref: PromptSchemaRef
    output_schema_ref: PromptSchemaRef


class CreatePromptVersionRequest(ContractModel):
    content: str
    changelog: str | None = None


class ApprovePromptVersionRequest(ContractModel):
    reason: str


class PublishPromptVersionRequest(ContractModel):
    reason: str


class RollbackPromptRequest(ContractModel):
    target_version_id: str
    reason: str


class CreatePromptBindingRequest(ContractModel):
    prompt_template_id: str
    prompt_version_id: str
    case_id: str | None = None
    node_id: str | None = None
    priority: int


class PatchPromptBindingRequest(ContractModel):
    prompt_version_id: str | None = None
    enabled: bool | None = None
    priority: int | None = None


class PromptExperimentScope(ContractModel):
    case_id: str | None = None
    node_id: str | None = None


class PromptExperiment(EntityMeta):
    prompt_template_id: str
    variants: list[str]
    traffic_split: dict[str, float]
    scope: PromptExperimentScope
    status: Literal["draft", "running", "stopped", "completed"] = "draft"
    start_at: datetime | None = None
    end_at: datetime | None = None


class PromptExperimentQuery(BaseListQuery):
    prompt_template_id: str | None = None
    status: str | None = None


class CreatePromptExperimentRequest(ContractModel):
    prompt_template_id: str
    variants: list[str]
    traffic_split: dict[str, float]
    scope: PromptExperimentScope
    start_at: datetime | None = None
    end_at: datetime | None = None


class PatchPromptExperimentRequest(ContractModel):
    status: Literal["draft", "running", "stopped", "completed"] | None = None
    traffic_split: dict[str, float] | None = None
    end_at: datetime | None = None
