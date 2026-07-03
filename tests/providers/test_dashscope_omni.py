from __future__ import annotations

import json

import httpx
import pytest

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderRuntimeError
from packages.ai.providers.dashscope import DashScopeOmniProvider
from packages.core.contracts import ProviderOptionsSchemaRef, ProviderProfile
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore


def _sse(lines: list[dict], *, usage: dict | None = None) -> bytes:
    chunks = [f"data: {json.dumps(piece, ensure_ascii=False)}\n\n" for piece in lines]
    if usage is not None:
        chunks.append(
            f"data: {json.dumps({'choices': [], 'usage': usage}, ensure_ascii=False)}\n\n"
        )
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode()


def _context(tmp_path, *, model_id: str = "qwen3.5-omni-plus") -> ProviderInvocationContext:
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    secret_ref = secret_store.put("dashscope-key", secret_ref="dashscope_prod.secret")
    profile = ProviderProfile(
        id="dashscope.omni.test",
        provider_id="dashscope.omni",
        model_id=model_id,
        capability="audio.understanding",
        display_name="DashScope Omni Test",
        environment="prod",
        secret_ref=secret_ref,
        timeout_sec=60,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.audio.options"),
    )
    repository.provider_profiles[profile.id] = profile
    return ProviderInvocationContext(
        repository=repository,
        profile=profile,
        invocation_id="pinv_omni_test",
        secret_store=secret_store,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )


def test_omni_streams_and_parses_json(tmp_path):
    body = _sse(
        [
            {"choices": [{"delta": {"content": "{\"mood\":"}}]},
            {"choices": [{"delta": {"content": " \"燃\", \"scene_fit\": [\"高光\"]}"}}]},
        ],
        usage={"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["modalities"] == ["text"]
        assert payload["model"] == "qwen3.5-omni-plus"
        content = payload["messages"][0]["content"]
        assert any(part.get("type") == "input_audio" for part in content)
        audio_part = next(part for part in content if part.get("type") == "input_audio")
        assert audio_part["input_audio"]["data"] == "https://x/clip.mp3"
        assert audio_part["input_audio"]["format"] == "mp3"
        return httpx.Response(200, content=body)

    provider = DashScopeOmniProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    ctx = _context(tmp_path)
    call = ProviderCall(
        case_id="c",
        provider_profile_id=ctx.profile.id,
        capability_id="audio.understanding",
        input={"prompt": "标注这段BGM", "audio_uri": "https://x/clip.mp3"},
        idempotency_key="k",
    )

    result = provider.invoke_with_context(call, ctx)

    assert result.output["content"] == '{"mood": "燃", "scene_fit": ["高光"]}'
    assert result.output["intent"]["mood"] == "燃"
    assert result.output["intent"]["scene_fit"] == ["高光"]
    assert result.input_tokens == 21
    assert result.output_tokens == 8
    assert result.raw_usage["provider_response"]["streamed"] is True
    assert result.raw_usage["provider_response"]["usage"]["total_tokens"] == 29


def test_omni_rejects_wrong_capability(tmp_path):
    provider = DashScopeOmniProvider(httpx.Client())
    ctx = _context(tmp_path)
    call = ProviderCall(
        case_id="c",
        provider_profile_id=ctx.profile.id,
        capability_id="llm.chat",
        input={},
        idempotency_key="k",
    )

    with pytest.raises(Exception):
        provider.invoke_with_context(call, ctx)


def test_omni_requires_audio_uri_when_messages_are_not_supplied(tmp_path):
    provider = DashScopeOmniProvider(httpx.Client())
    ctx = _context(tmp_path)
    call = ProviderCall(
        case_id="c",
        provider_profile_id=ctx.profile.id,
        capability_id="audio.understanding",
        input={"prompt": "标注"},
        idempotency_key="k",
    )

    with pytest.raises(ProviderRuntimeError) as exc:
        provider.invoke_with_context(call, ctx)

    assert exc.value.code.value == "provider.unsupported_option"
    assert "audio_uri" in exc.value.message


def test_omni_http_error_surfaces_remote_failure(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="bad gateway")

    provider = DashScopeOmniProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    ctx = _context(tmp_path)
    call = ProviderCall(
        case_id="c",
        provider_profile_id=ctx.profile.id,
        capability_id="audio.understanding",
        input={
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "input_audio", "input_audio": {"data": "https://x/a.wav"}}],
                }
            ]
        },
        idempotency_key="k",
    )

    with pytest.raises(ProviderRuntimeError) as exc:
        provider.invoke_with_context(call, ctx)

    assert exc.value.code.value == "provider.remote_failed"
    assert "DashScope Omni HTTP 500" in exc.value.message
