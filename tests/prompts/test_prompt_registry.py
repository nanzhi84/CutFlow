import pytest
from fastapi.testclient import TestClient

from apps.api.main import app, repository
from packages.ai.prompts.registry import PromptRegistry
from packages.core.contracts import (
    ErrorCode,
    PromptBinding,
    PromptSchemaRef,
    PromptTemplate,
    PromptVersion,
)
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError


def test_prompt_render_requires_all_variables():
    repository = Repository()
    template = PromptTemplate(
        id="prompt_test",
        name="Variable Test",
        purpose="test",
        variables_schema_ref=PromptSchemaRef(schema_id="test.variables"),
        output_schema_ref=PromptSchemaRef(schema_id="test.output"),
        status="active",
    )
    version = PromptVersion(
        id="prompt_test_v1",
        prompt_template_id=template.id,
        content="Hello {name}, {missing}",
        status="published",
    )
    binding = PromptBinding(
        id="binding_test",
        prompt_template_id=template.id,
        prompt_version_id=version.id,
        node_id="TestNode",
        priority=1,
    )
    repository.prompt_templates[template.id] = template
    repository.prompt_versions[version.id] = version
    repository.prompt_bindings[binding.id] = binding
    registry = PromptRegistry(repository)

    with pytest.raises(NodeExecutionError) as exc:
        registry.render(node_id="TestNode", variables={"name": "Ada"})
    assert exc.value.error.code == ErrorCode.prompt_render_error


def test_prompt_output_schema_validation_rejects_invalid_creative_intent():
    repository = Repository()
    registry = PromptRegistry(repository)
    with pytest.raises(NodeExecutionError) as exc:
        registry.validate_output(
            prompt_version_id="prompt_creative_intent_v1",
            output={"intent": {"hook": "ok"}},
        )
    assert exc.value.error.code == ErrorCode.prompt_output_invalid


def test_prompt_publish_and_rollback_api_flow():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text
    template = client.post(
        "/api/prompts",
        json={
            "name": "Ops prompt",
            "purpose": "ops.test",
            "variables_schema_ref": {"schema_id": "ops.variables", "schema_version": "v1"},
            "output_schema_ref": {"schema_id": "ops.output", "schema_version": "v1"},
        },
    ).json()["template"]
    version = client.post(
        f"/api/prompts/{template['id']}/versions",
        json={"content": "hello", "changelog": "initial"},
    ).json()["version"]
    approved = client.post(
        f"/api/prompts/{template['id']}/versions/{version['id']}/approve",
        json={"reason": "reviewed"},
    ).json()["version"]
    assert approved["status"] == "approved"
    published = client.post(
        f"/api/prompts/{template['id']}/versions/{version['id']}/publish",
        json={"reason": "ship"},
    ).json()["version"]
    assert published["status"] == "published"
    rolled_back = client.post(
        f"/api/prompts/{template['id']}/rollback",
        json={"target_version_id": version["id"], "reason": "verify rollback"},
    ).json()["version"]
    assert rolled_back["status"] == "published"


def test_prompt_invocation_links_to_provider_invocation_in_video_workflow():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text
    response = client.post(
        "/api/jobs/digital-human-video",
        json={
            "case_id": "case_demo",
            "title": "Prompt linkage",
            "script": "验证 prompt 调用和 provider 调用的关联。",
            "voice": {"voice_id": "voice_sandbox"},
            "portrait": {"template_mode": "agent"},
            "strictness": {"strict_timestamps": False},
        },
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["initial_run"]["id"]
    linked = [
        invocation
        for invocation in repository().prompt_invocations.values()
        if invocation.run_id == run_id and invocation.provider_invocation_id
    ]
    assert linked
