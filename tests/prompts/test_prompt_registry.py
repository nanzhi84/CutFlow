import pytest
from fastapi.testclient import TestClient

from apps.api.main import app, repository
from packages.ai.prompts.registry import PromptRegistry, extract_script_title_from_output
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


def test_prompt_render_replaces_double_braced_variables():
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
        content="产品：{{product_name}}\n输出示例：{{\"script\":\"...\"}}",
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

    _invocation, rendered = registry.render(
        node_id="TestNode",
        variables={"product_name": "旭通超市"},
    )

    assert "产品：旭通超市" in rendered
    assert "{旭通超市}" not in rendered
    assert '{{"script":"..."}}' in rendered


def test_prompt_output_schema_validation_rejects_invalid_creative_intent():
    repository = Repository()
    registry = PromptRegistry(repository)
    with pytest.raises(NodeExecutionError) as exc:
        registry.validate_output(
            prompt_version_id="prompt_creative_intent_v1",
            output={"intent": {"hook": "ok"}},
        )
    assert exc.value.error.code == ErrorCode.prompt_output_invalid


@pytest.mark.parametrize(
    "bad_output",
    [
        {},
        {"script": ""},
        {"script": "   "},
        {"items": []},
        {"items": [{"title": "t", "publish_content": "p"}]},
        # Structured JSON content (items/object) with no usable script = a failed
        # reply; the raw JSON string must NOT silently become the "script".
        {"content": '{"items": [{"title": "只有标题"}]}'},
        {"content": "{}"},
        "plain string is not an object",
    ],
)
def test_script_output_schema_validation_rejects_empty_script(bad_output):
    # case_agent_script.output (provider_seed CaseAgentScriptGenerate template).
    repository = Repository()
    registry = PromptRegistry(repository)
    with pytest.raises(NodeExecutionError) as exc:
        registry.validate_output(
            prompt_version_id="prompt_case_agent_script_v1",
            output=bad_output,
        )
    assert exc.value.error.code == ErrorCode.prompt_output_invalid


@pytest.mark.parametrize(
    "good_output",
    [
        {"script": "完整的口播脚本文本。"},
        {"items": [{"script": "第一条脚本。"}]},
        {"items": [{"content": "第一条脚本。"}]},
        {"content": '{"script": "嵌在 content 里的脚本。"}'},
        {"content": '{"items": [{"script": "嵌套 items 脚本。"}]}'},
        # Plain-prose content: the model ignored the JSON contract and wrote the
        # script as text; the trimmed text IS the script (tolerant for real LLMs).
        {"content": "数字人主播从头到尾念出来的口播台词。"},
        {"draft": "草稿脚本。"},
    ],
)
def test_script_output_schema_validation_accepts_usable_script(good_output):
    repository = Repository()
    registry = PromptRegistry(repository)
    # Must not raise.
    registry.validate_output(
        prompt_version_id="prompt_case_agent_script_v1",
        output=good_output,
    )


def test_script_variant_output_schema_validation_uses_prompt_script_output():
    # prompt.script.output schema id (the migrated per-variant templates resolve to
    # these). Reuse the same non-empty-script contract.
    repository = Repository()
    registry = PromptRegistry(repository)
    with pytest.raises(NodeExecutionError) as exc:
        registry.validate_output(
            prompt_version_id="prompt_script_hard_ad_fresh_generate_v1",
            output={"items": [{"title": "no script here"}]},
        )
    assert exc.value.error.code == ErrorCode.prompt_output_invalid
    # And the valid shape passes.
    registry.validate_output(
        prompt_version_id="prompt_script_hard_ad_fresh_generate_v1",
        output={"items": [{"script": "脚本。"}]},
    )


def test_extract_script_title_from_nested_output():
    assert (
        extract_script_title_from_output(
            {"content": '{"items": [{"title": "海风小镇便利钩子", "script": "脚本。"}]}'}
        )
        == "海风小镇便利钩子"
    )


def test_ai_cover_prompt_resolves_through_registry_binding():
    # Spec §10.1: the AI cover prompt must be reachable via a node binding, not only
    # by a hardcoded version id. The default seed now binds PublishCover.ai_cover.
    repository = Repository()
    registry = PromptRegistry(repository)
    binding, version = registry.resolve_published_version(node_id="PublishCover.ai_cover")
    assert binding.node_id == "PublishCover.ai_cover"
    assert binding.prompt_template_id == "prompt_cover_ai_cover"
    assert version.id == "prompt_cover_ai_cover_v1"
    assert version.status == "published"


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
