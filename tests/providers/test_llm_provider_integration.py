from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderResult
from packages.core.provider_idempotency import is_provider_call_idempotency_key
from packages.core.contracts import (
    CreateProviderProfileRequest,
    ProviderOptionsSchemaRef,
)
from packages.core.storage.database import (
    PromptBindingRow,
    PromptTemplateRow,
    PromptVersionRow,
    ScriptDraftRow,
)


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _arm_llm_profile(client: TestClient, provider_id: str = "fake.llm"):
    """Persist a real (non-sandbox) ``llm.chat`` provider profile in Postgres.

    The provider gateway resolves profiles through its SQL ``provider_reader`` (the
    in-memory ``app.state.repository`` is only the workflow run-state base, no longer
    a storage backend), so the profile must live in the database for the case-agent /
    creative-intent capability lookup to pick it up. Returns the persisted profile so
    callers can assert against its server-assigned id.
    """
    return client.app.state.sqlalchemy_provider_repository.create_profile(
        CreateProviderProfileRequest(
            provider_id=provider_id,
            model_id="fake-chat",
            capability="llm.chat",
            display_name="Fake LLM",
            environment="local",
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.options"),
        )
    )


class FakeLLMProvider:
    provider_id = "fake.llm"

    def __init__(self) -> None:
        self.calls: list[ProviderCall] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        if "brief" in call.input:
            return ProviderResult(output={"script": "Provider generated script with case memory."})
        return ProviderResult(
            output={
                "intent": {
                    "hook": "provider hook",
                    "tone": "clear",
                    "audience": "operators",
                    "beats": ["provider beat"],
                }
            }
        )


def test_case_agent_generate_with_memory_uses_real_llm_profile():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = FakeLLMProvider()
        client.app.state.provider_gateway.register(provider)
        profile = _arm_llm_profile(client)

        response = client.post(
            "/api/cases/case_demo/scripts/generate-with-memory",
            json={"brief": "Polish this script.", "memory_ids": []},
        )

        assert response.status_code == 202, response.text
        assert response.json()["script"] == "Provider generated script with case memory."
        assert provider.calls
        assert provider.calls[0].provider_profile_id == profile.id
        prompt_invocations = list(repository.prompt_invocations.values())
        assert prompt_invocations[-1].provider_invocation_id


def test_case_agent_generation_prompt_appends_brief_and_recent_scripts_for_variant_prompt():
    with TestClient(create_app()) as client:
        _login_admin(client)
        provider = FakeLLMProvider()
        client.app.state.provider_gateway.register(provider)
        _arm_llm_profile(client)
        # Bind a published variant prompt + a recent draft in Postgres so the
        # variant node path resolves and the recency-aware context is built from
        # real persisted scripts (prompt registry + case-learning repo read SQL).
        with client.app.state.sqlalchemy_session_factory() as session:
            session.add(
                PromptTemplateRow(
                    id="prompt_script_variant_test",
                    name="Script Variant Test",
                    purpose="prompt.script.hard_ad.fresh",
                    variables_schema_ref={
                        "schema_id": "prompt.script.variables",
                        "schema_version": "v1",
                    },
                    output_schema_ref={
                        "schema_id": "prompt.script.output",
                        "schema_version": "v1",
                    },
                    status="active",
                )
            )
            session.add(
                PromptVersionRow(
                    id="prompt_script_variant_test_v1",
                    prompt_template_id="prompt_script_variant_test",
                    content="只看产品：{product_name}",
                    status="published",
                )
            )
            session.flush()  # satisfy the binding's FK to template + version
            session.add(
                PromptBindingRow(
                    id="prompt_binding_variant_test",
                    prompt_template_id="prompt_script_variant_test",
                    prompt_version_id="prompt_script_variant_test_v1",
                    node_id="CaseAgentScriptGenerate.hard_ad.fresh",
                    priority=1,
                    enabled=True,
                )
            )
            session.add(
                ScriptDraftRow(
                    id="draft_recent",
                    case_id="case_demo",
                    title="旧草稿",
                    script="旧开场不要重复，旧结构也不要重复。",
                    status="draft",
                    memory_ids=[],
                )
            )
            session.commit()

        response = client.post(
            "/api/cases/case_demo/scripts/generate-with-memory",
            json={
                "brief": "请生成一版全新脚本。\n版本序号：2",
                "memory_ids": [],
                "persona_mode": "hard_ad",
                "operation": "fresh",
                "strategy_tags": ["开场钩子"],
                "variation_count": 1,
            },
        )

        assert response.status_code == 202, response.text
        assert response.json()["title"] == "硬广 · 全新创作脚本"
        prompt = provider.calls[0].input["prompt"]
        assert "【本轮用户要求】" in prompt
        assert "版本序号：2" in prompt
        assert "【历史避重要求】" in prompt
        assert "旧开场不要重复" in prompt
        assert "【策略标签】开场钩子" in prompt


class _InvalidThenValidLLMProvider:
    """Returns a malformed-but-non-empty script reply, then a valid one.

    Exercises the no-silent-degrade retry: the first reply has no usable script
    (output_invalid -> retry), the second carries a real script."""

    provider_id = "fake.llm"

    def __init__(self, invalid_replies: int) -> None:
        self.calls: list[ProviderCall] = []
        self._invalid_left = invalid_replies

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        if self._invalid_left > 0:
            self._invalid_left -= 1
            # Non-empty JSON-ish content but no usable script field.
            return ProviderResult(output={"content": '{"items": [{"title": "只有标题"}]}'})
        return ProviderResult(output={"script": "重试后生成的可用脚本。"})


def test_script_generation_retries_on_output_invalid_then_succeeds():
    with TestClient(create_app()) as client:
        _login_admin(client)
        provider = _InvalidThenValidLLMProvider(invalid_replies=1)
        client.app.state.provider_gateway.register(provider)
        _arm_llm_profile(client)

        response = client.post(
            "/api/cases/case_demo/scripts/generate-with-memory",
            json={"brief": "Generate a script.", "memory_ids": []},
        )

        assert response.status_code == 202, response.text
        assert response.json()["script"] == "重试后生成的可用脚本。"
        # One invalid + one valid = exactly two provider calls.
        assert len(provider.calls) == 2


def test_script_generation_hard_fails_with_prompt_output_invalid_after_exhaustion():
    with TestClient(create_app()) as client:
        _login_admin(client)
        # Always invalid: never yields a usable script -> hard_fail after retries.
        provider = _InvalidThenValidLLMProvider(invalid_replies=99)
        client.app.state.provider_gateway.register(provider)
        _arm_llm_profile(client)

        response = client.post(
            "/api/cases/case_demo/scripts/generate-with-memory",
            json={"brief": "Generate a script.", "memory_ids": []},
        )

        assert response.status_code >= 400, response.text
        body = response.json()
        assert body.get("error", {}).get("code") == "prompt.output_invalid", body
        # 1 initial + 2 retries = 3 bounded attempts, no infinite loop.
        assert len(provider.calls) == 3


def test_creative_intent_prefers_real_llm_profile_over_sandbox():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = FakeLLMProvider()
        client.app.state.provider_gateway.register(provider)
        _arm_llm_profile(client)

        response = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "Provider LLM",
                "script": "Use the real LLM profile for intent.",
                "voice": {"voice_id": "voice_sandbox"},
                "broll": {"enabled": False},
                "bgm": {"enabled": False},
                "subtitle": {"enabled": True},
                "lipsync": {"enabled": False},
                "strictness": {"strict_timestamps": False},
            },
        )

        assert response.status_code == 201, response.text
        run_id = response.json()["initial_run"]["id"]
        resolve_node = next(
            node for node in repository.node_runs[run_id] if node.node_id == "ResolveCreativeIntent"
        )
        llm_invocations = [
            item for item in repository.provider_invocations.values() if item.capability_id == "llm.chat"
        ]
        assert llm_invocations[-1].provider_id == "fake.llm"
        assert provider.calls
        # Run-scoped stable key: excludes node_run.id so an infrastructure retry reuses it.
        assert is_provider_call_idempotency_key(provider.calls[0].idempotency_key)
        assert resolve_node.id not in provider.calls[0].idempotency_key
