from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderResult
from packages.core.contracts import ProviderOptionsSchemaRef, ProviderProfile


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _llm_profile(provider_id: str = "fake.llm") -> ProviderProfile:
    return ProviderProfile(
        id=f"{provider_id}.default",
        provider_id=provider_id,
        model_id="fake-chat",
        capability="llm.chat",
        display_name="Fake LLM",
        environment="local",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.options"),
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
        profile = _llm_profile()
        repository.provider_profiles[profile.id] = profile

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
        repository = client.app.state.repository
        provider = _InvalidThenValidLLMProvider(invalid_replies=1)
        client.app.state.provider_gateway.register(provider)
        profile = _llm_profile()
        repository.provider_profiles[profile.id] = profile

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
        repository = client.app.state.repository
        # Always invalid: never yields a usable script -> hard_fail after retries.
        provider = _InvalidThenValidLLMProvider(invalid_replies=99)
        client.app.state.provider_gateway.register(provider)
        profile = _llm_profile()
        repository.provider_profiles[profile.id] = profile

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
        profile = _llm_profile()
        repository.provider_profiles[profile.id] = profile

        response = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "Provider LLM",
                "script": "Use the real LLM profile for intent.",
                "voice": {"voice_id": "voice_sandbox"},
                "portrait": {"template_mode": "agent"},
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
        assert provider.calls[0].idempotency_key == f"{run_id}:{resolve_node.id}:resolve_creative_intent"
