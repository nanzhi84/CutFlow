from __future__ import annotations


from fastapi import Request

from apps.api.common import (
    prompt_repository,
    request_id,
)
from packages.core import contracts as c

def list_prompts(
    request: Request,
    limit: int = 50,
    status: str | None = None,
    purpose: str | None = None,
) -> c.PageResponse[c.PromptTemplateView]:
    values = prompt_repository(request).list_templates(status=status, purpose=purpose, limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_prompt(payload: c.CreatePromptTemplateRequest, request: Request) -> c.PromptTemplateView:
    return prompt_repository(request).create_template(payload)


def prompt_versions(request: Request, template_id: str, limit: int = 50) -> c.PageResponse[c.PromptVersionView]:

    values = prompt_repository(request).list_versions(template_id, limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_prompt_version(
    template_id: str, payload: c.CreatePromptVersionRequest, request: Request
) -> c.PromptVersionView:
    return prompt_repository(request).create_version(template_id, payload)


def approve_prompt_version(
    template_id: str, version_id: str, payload: c.ApprovePromptVersionRequest, request: Request
) -> c.PromptVersionView:
    return prompt_repository(request).approve_version(template_id, version_id, payload)


def publish_prompt_version(
    template_id: str, version_id: str, payload: c.PublishPromptVersionRequest, request: Request
) -> c.PromptVersionView:
    return prompt_repository(request).publish_version(template_id, version_id, payload)


def rollback_prompt(
    template_id: str, payload: c.RollbackPromptRequest, request: Request
) -> c.PromptVersionView:
    return prompt_repository(request).rollback(template_id, payload)


def prompt_bindings(request: Request, limit: int = 50) -> c.PageResponse[c.PromptBindingView]:

    values = prompt_repository(request).list_bindings(limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_prompt_binding(payload: c.CreatePromptBindingRequest, request: Request) -> c.PromptBindingView:
    return prompt_repository(request).create_binding(payload)


def patch_prompt_binding(
    binding_id: str, payload: c.PatchPromptBindingRequest, request: Request
) -> c.PromptBindingView:
    return prompt_repository(request).patch_binding(binding_id, payload)


def prompt_experiments(
    request: Request,
    limit: int = 50,
    prompt_template_id: str | None = None,
    status: str | None = None,
) -> c.PageResponse[c.PromptExperiment]:
    values = prompt_repository(request).list_experiments(
        prompt_template_id=prompt_template_id,
        status=status,
        limit=limit,
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_prompt_experiment(
    payload: c.CreatePromptExperimentRequest, request: Request
) -> c.PromptExperiment:
    return prompt_repository(request).create_experiment(payload)


def patch_prompt_experiment(
    experiment_id: str, payload: c.PatchPromptExperimentRequest, request: Request
) -> c.PromptExperiment:
    return prompt_repository(request).patch_experiment(experiment_id, payload)
