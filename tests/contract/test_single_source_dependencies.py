from pathlib import Path


def test_gateway_and_prompt_registry_do_not_define_module_singletons():
    gateway_source = Path("packages/ai/gateway/provider_gateway.py").read_text(encoding="utf-8")
    prompt_source = Path("packages/ai/prompts/registry.py").read_text(encoding="utf-8")

    assert "_GATEWAY" not in gateway_source
    assert "get_repository()" not in gateway_source
    assert "_REGISTRY" not in prompt_source
    assert "get_repository()" not in prompt_source


def test_api_main_repository_and_workflow_are_app_state_dependencies():
    source = Path("apps/api/main.py").read_text(encoding="utf-8")

    assert "repo: Repository = get_repository()" not in source
    assert "workflow = get_digital_human_workflow()" not in source
    assert "app.state.repository" in source
    assert "app.state.workflow" in source
