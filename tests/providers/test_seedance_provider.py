from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderGateway,
    ProviderRuntimeError,
    SandboxProvider,
)
from packages.ai.providers import seedance as seedance_mod
from packages.core.contracts import (
    ErrorCode,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore

_BASE = "https://ark.cn-beijing.volces.com/api/v3"
_TASKS = "/api/v3/contents/generations/tasks"


def _gateway(tmp_path, transport: httpx.MockTransport) -> tuple[Repository, ProviderGateway]:
    repository = Repository()
    gateway = ProviderGateway(
        repository,
        secret_store=LocalSecretStore(tmp_path / "secrets"),
        object_store=LocalObjectStore(tmp_path / "objects"),
        http_client=httpx.Client(transport=transport),
    )
    return repository, gateway


def _profile(
    repository: Repository,
    secret_ref: str,
    *,
    model_id: str = "doubao-seedance-2-0-260128",
    default_options: dict | None = None,
) -> ProviderProfile:
    options = {"base_url": _BASE, "poll_interval": 0, "poll_max_attempts": 3}
    if default_options:
        options.update(default_options)
    profile = ProviderProfile(
        id="volcengine.seedance.test",
        provider_id="volcengine.seedance",
        model_id=model_id,
        capability="video.generate",
        display_name="Seedance test",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.video.options"),
        default_options=options,
    )
    repository.provider_profiles[profile.id] = profile
    return profile


def test_seedance_text_to_video_submits_polls_and_stores(tmp_path, media_fixture_factory):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="seedance.mp4")
    submitted_body: dict = {}
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path == _TASKS and request.method == "POST":
            assert request.headers["authorization"] == "Bearer ark-key"
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-1"})
        if request.url.path == f"{_TASKS}/cgt-1":
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(200, json={"id": "cgt-1", "status": "running"})
            return httpx.Response(
                200,
                json={
                    "id": "cgt-1",
                    "status": "succeeded",
                    "content": {"video_url": "https://files.example/seedance-result.mp4"},
                    "usage": {"completion_tokens": 324900, "total_tokens": 324900},
                },
            )
        if str(request.url) == "https://files.example/seedance-result.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("ark-key")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref)

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={"prompt": "门头特写，暖光", "duration_sec": 15, "ratio": "9:16", "resolution": "720p"},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert invocation.external_job_id == "cgt-1"
    assert invocation.output_tokens == 324900
    assert invocation.usage and invocation.usage.output_tokens == 324900
    assert result is not None
    assert result.output["external_job_id"] == "cgt-1"
    assert result.output_tokens == 324900
    assert result.video_seconds == 15.0
    # Seedance 2.0 param style: top-level JSON fields + native audio on.
    assert submitted_body["ratio"] == "9:16"
    assert submitted_body["resolution"] == "720p"
    assert submitted_body["duration"] == 15
    assert submitted_body["generate_audio"] is True
    # The prompt rides in content[0].text (here passed through verbatim).
    assert submitted_body["content"][0]["type"] == "text"
    assert "门头特写，暖光" in submitted_body["content"][0]["text"]
    artifact = repository.artifacts[result.output["video_artifact_id"]]
    assert artifact.size_bytes == result_video.stat().st_size
    assert artifact.media_info and artifact.media_info.media_type == "video"


def test_seedance_access_key_secret_gets_temporary_api_key_then_submits(
    tmp_path, media_fixture_factory
):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="seedance-ak.mp4")
    submitted_body: dict = {}
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://files.example/seedance-ak.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        auth = request.headers.get("authorization", "")
        if request.url.host == "ark.cn-beijing.volcengineapi.com":
            seen_auth.append(auth)
            assert request.url.path == "/"
            assert str(request.url.query, "utf-8") == "Action=GetApiKey&Version=2024-01-01"
            assert auth.startswith("HMAC-SHA256 Credential=AKLTxxx/")
            assert "/cn-beijing/ark/request" in auth
            assert "sk-secret" not in auth
            assert "x-content-sha256" in request.headers
            assert "x-date" in request.headers
            payload = json.loads(request.content)
            assert payload["DurationSeconds"] == 604800
            assert payload["ResourceType"] == "endpoint"
            assert payload["ResourceIds"] == ["ep-seedance"]
            return httpx.Response(
                200,
                json={
                    "ResponseMetadata": {"Action": "GetApiKey"},
                    "Result": {"ApiKey": "tmp-ark-key", "ExpiredTime": 0},
                },
            )
        if request.url.path == _TASKS and request.method == "POST":
            assert auth == "Bearer tmp-ark-key"
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-ak"})
        if request.url.path == f"{_TASKS}/cgt-ak":
            assert auth == "Bearer tmp-ark-key"
            return httpx.Response(
                200,
                json={
                    "id": "cgt-ak",
                    "status": "succeeded",
                    "content": {"video_url": "https://files.example/seedance-ak.mp4"},
                },
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref, model_id="ep-seedance")

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={"prompt": "门头特写，暖光"},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["external_job_id"] == "cgt-ak"
    assert submitted_body["model"] == "ep-seedance"
    assert submitted_body["content"][0]["text"] == "门头特写，暖光"
    assert len(seen_auth) == 1


def test_seedance_access_key_model_id_uses_presetendpoint(tmp_path, media_fixture_factory):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="seedance-preset.mp4")
    submitted_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if request.url.host == "ark.cn-beijing.volcengineapi.com":
            payload = json.loads(request.content)
            assert payload["DurationSeconds"] == 604800
            assert payload["ResourceType"] == "presetendpoint"
            assert payload["ResourceIds"] == ["doubao-seedance-2-0-260128"]
            assert payload["ProjectName"] == "default"
            assert auth.startswith("HMAC-SHA256 Credential=AKLTxxx/")
            return httpx.Response(
                200,
                json={
                    "ResponseMetadata": {"Action": "GetApiKey"},
                    "Result": {"ApiKey": "tmp-preset-key", "ExpiredTime": 0},
                },
            )
        if request.url.path == _TASKS and request.method == "POST":
            assert auth == "Bearer tmp-preset-key"
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-preset"})
        if request.url.path == f"{_TASKS}/cgt-preset":
            assert auth == "Bearer tmp-preset-key"
            return httpx.Response(
                200,
                json={
                    "id": "cgt-preset",
                    "status": "succeeded",
                    "content": {"video_url": "https://files.example/seedance-preset.mp4"},
                },
            )
        if str(request.url) == "https://files.example/seedance-preset.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref, model_id="doubao-seedance-2-0-260128")

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={"prompt": "门头特写，暖光"},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert submitted_body["model"] == "doubao-seedance-2-0-260128"


def test_seedance_direct_signed_auth_mode_signs_submit_and_poll(
    tmp_path, media_fixture_factory
):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="seedance-signed.mp4")
    submitted_body: dict = {}
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://files.example/seedance-signed.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        auth = request.headers.get("authorization", "")
        seen_auth.append(auth)
        assert auth.startswith("HMAC-SHA256 Credential=AKLTxxx/")
        assert "/cn-beijing/ark/request" in auth
        assert "sk-secret" not in auth
        assert "x-content-sha256" in request.headers
        assert "x-date" in request.headers
        if request.url.path == _TASKS and request.method == "POST":
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-signed"})
        if request.url.path == f"{_TASKS}/cgt-signed":
            return httpx.Response(
                200,
                json={
                    "id": "cgt-signed",
                    "status": "succeeded",
                    "content": {"video_url": "https://files.example/seedance-signed.mp4"},
                },
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        secret_ref,
        model_id="ep-seedance",
        default_options={"auth_type": "signed"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={"prompt": "门头特写，暖光"},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["external_job_id"] == "cgt-signed"
    assert submitted_body["model"] == "ep-seedance"
    assert len(seen_auth) >= 2


def test_seedance_reference_image_goes_into_content(tmp_path, media_fixture_factory):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="seedance.mp4")
    submitted_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _TASKS and request.method == "POST":
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-9"})
        if request.url.path == f"{_TASKS}/cgt-9":
            return httpx.Response(
                200,
                json={"id": "cgt-9", "status": "succeeded", "content": {"video_url": "https://files.example/r.mp4"}},
            )
        if str(request.url) == "https://files.example/r.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("ark-key")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref)

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={
                "prompt": "保持人物一致",
                "references": [{"uri": "https://cdn.example/owner.jpg", "kind": "image"}],
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    image_entries = [c for c in submitted_body["content"] if c.get("type") == "image_url"]
    assert image_entries == [
        {"type": "image_url", "image_url": {"url": "https://cdn.example/owner.jpg"}, "role": "reference_image"}
    ]


def test_seedance_video_reference_goes_into_content(tmp_path, media_fixture_factory):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="seedance.mp4")
    submitted_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _TASKS and request.method == "POST":
            submitted_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-v"})
        if request.url.path == f"{_TASKS}/cgt-v":
            return httpx.Response(
                200,
                json={"id": "cgt-v", "status": "succeeded", "content": {"video_url": "https://files.example/v.mp4"}},
            )
        if str(request.url) == "https://files.example/v.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("ark-key")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref)

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={
                "prompt": "老板娘出镜口播",
                "references": [{"uri": "https://cdn.example/owner.mp4", "kind": "video"}],
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    video_entries = [c for c in submitted_body["content"] if c.get("type") == "video_url"]
    assert video_entries == [
        {"type": "video_url", "video_url": {"url": "https://cdn.example/owner.mp4"}, "role": "reference_video"}
    ]


def test_seedance_access_key_getapikey_http_403_maps_to_auth_failed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ark.cn-beijing.volcengineapi.com":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref, model_id="ep-seedance")

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={"prompt": "x"},
        )
    )

    assert result is None
    assert invocation.status == ProviderStatus.failed
    assert invocation.error and invocation.error.code == ErrorCode.provider_auth_failed


def test_seedance_access_key_invalid_resource_maps_to_unsupported_option(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ark.cn-beijing.volcengineapi.com":
            return httpx.Response(
                200,
                json={
                    "ResponseMetadata": {
                        "Error": {"Code": "InvalidParameter.ResourceIds", "Message": "bad"}
                    }
                },
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref, model_id="ep-seedance")

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={"prompt": "x"},
        )
    )

    assert result is None
    assert invocation.status == ProviderStatus.failed
    assert invocation.error and invocation.error.code == ErrorCode.provider_unsupported_option


def test_seedance_local_reference_fails_loudly(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        # Must never reach submit: presign fails before any network call.
        return httpx.Response(500, text="should not be called")

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("ark-key")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref)

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={
                "prompt": "x",
                "references": [{"uri": "local://objects/ref.jpg", "role": "reference_image"}],
            },
        )
    )

    assert result is None
    assert invocation.status == ProviderStatus.failed
    assert invocation.error and invocation.error.code == ErrorCode.provider_unsupported_option


def test_seedance_failed_task_surfaces_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _TASKS and request.method == "POST":
            return httpx.Response(200, json={"id": "cgt-2"})
        if request.url.path == f"{_TASKS}/cgt-2":
            return httpx.Response(200, json={"id": "cgt-2", "status": "failed", "error": {"code": "content_policy"}})
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("ark-key")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref)

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={"prompt": "x"},
        )
    )

    assert result is None
    assert invocation.status == ProviderStatus.failed
    assert invocation.error and invocation.error.code == ErrorCode.provider_remote_failed


def test_seedance_validation_submit_timeout_and_missing_result(tmp_path, monkeypatch):
    monkeypatch.setattr(seedance_mod.time, "sleep", lambda _seconds: None)

    def invoke(handler, *, default_options: dict | None = None, input_payload: dict | None = None):
        repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
        secret_ref = gateway.secret_store.put("ark-key")  # type: ignore[union-attr]
        profile = _profile(repository, secret_ref, default_options=default_options)
        return gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="video.generate",
                input=input_payload if input_payload is not None else {"prompt": "门头特写"},
            )
        )

    invocation, result = invoke(
        lambda request: httpx.Response(500, text="should not call"),
        input_payload={},
    )
    assert result is None
    assert invocation.error and invocation.error.message == "Seedance generation requires a prompt."

    invocation, result = invoke(lambda request: httpx.Response(200, json={}))
    assert result is None
    assert invocation.error and invocation.error.message == "Seedance submit response missing task id."

    def running_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _TASKS:
            return httpx.Response(200, json={"id": "cgt-timeout"})
        return httpx.Response(200, json={"id": "cgt-timeout", "status": "running"})

    invocation, result = invoke(
        running_handler,
        default_options={"poll_interval": 0, "poll_max_attempts": 1},
    )
    assert result is None
    assert invocation.status == ProviderStatus.timed_out
    assert invocation.error and invocation.error.code == ErrorCode.provider_timeout

    def no_video_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _TASKS:
            return httpx.Response(200, json={"id": "cgt-no-video"})
        return httpx.Response(200, json={"id": "cgt-no-video", "status": "succeeded"})

    invocation, result = invoke(no_video_handler)
    assert result is None
    assert invocation.error and invocation.error.message == (
        "Seedance task succeeded but returned no video_url."
    )


def test_seedance_helper_edges_for_body_auth_resources_and_usage():
    body = seedance_mod.ArkSeedanceProvider._build_body(
        model_id="seedance-1",
        prompt="广告片",
        references=[{"type": "image_url", "image_url": {"url": "https://x/ref.png"}}],
        ratio="9:16",
        resolution="720p",
        duration=5,
        generate_audio=False,
        param_style="prompt_suffix",
    )
    assert body == {
        "model": "seedance-1",
        "content": [
            {"type": "text", "text": "广告片 --rt 9:16 --rs 720p --dur 5"},
            {"type": "image_url", "image_url": {"url": "https://x/ref.png"}},
        ],
        "generate_audio": False,
    }

    provider = seedance_mod.ArkSeedanceProvider(httpx.Client())
    call = ProviderCall(
        provider_profile_id="p",
        capability_id="video.generate",
        input={"references": ["bad", {"uri": ""}, {"uri": "https://x/ref.mp4", "kind": "video"}]},
    )
    context = SimpleNamespace(profile=SimpleNamespace(default_options={}))
    assert provider._reference_content(context, call) == [
        {"type": "video_url", "video_url": {"url": "https://x/ref.mp4"}, "role": "reference_video"}
    ]

    assert seedance_mod.ArkSeedanceProvider._result_video_url(
        {"data": {"content": {"videoUrl": "https://files.example/v.mp4"}}}
    ) == "https://files.example/v.mp4"
    assert seedance_mod.ArkSeedanceProvider._result_video_url({"content": {"url": "ftp://bad"}}) is None
    assert seedance_mod.ArkSeedanceProvider._usage_tokens(
        {"data": {"usage": {"totalTokens": 10, "outputTokens": 4}}}
    ) == (6, 4)
    assert seedance_mod.ArkSeedanceProvider._usage_tokens(
        {"usage": {"promptTokens": "-1", "completionTokens": "bad"}}
    ) == (0, 0)

    auto_context = SimpleNamespace(profile=SimpleNamespace(default_options={}))
    assert seedance_mod.ArkSeedanceProvider._use_access_key_auth("ak:sk", auto_context) is True
    assert seedance_mod.ArkSeedanceProvider._use_access_key_auth("api-key", auto_context) is False
    bearer_context = SimpleNamespace(profile=SimpleNamespace(default_options={"auth_type": "bearer"}))
    assert seedance_mod.ArkSeedanceProvider._use_access_key_auth("ak:sk", bearer_context) is False
    endpoint_context = SimpleNamespace(
        profile=SimpleNamespace(default_options={"endpoint_id": "ep-configured"}, model_id="")
    )
    assert seedance_mod.ArkSeedanceProvider._api_key_resource(endpoint_context) == (
        "endpoint",
        "ep-configured",
        None,
    )
    empty_context = SimpleNamespace(profile=SimpleNamespace(default_options={}, model_id=""))
    with pytest.raises(ProviderRuntimeError) as exc_info:
        seedance_mod.ArkSeedanceProvider._api_key_resource(empty_context)
    assert exc_info.value.code == ErrorCode.provider_unsupported_option


def test_seedance_openapi_error_mapping_and_api_key_cache(tmp_path, media_fixture_factory):
    def invoke(handler, *, model_id: str = "ep-seedance", default_options: dict | None = None):
        repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
        secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
        profile = _profile(
            repository,
            secret_ref,
            model_id=model_id,
            default_options=default_options,
        )
        return gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="video.generate",
                input={"prompt": "门头特写"},
            )
        )

    cases = [
        ("InvalidParameter.ProjectName", ErrorCode.provider_unsupported_option),
        ("AccessDenied", ErrorCode.provider_auth_failed),
        ("InternalError", ErrorCode.provider_remote_failed),
    ]
    for error_code, expected in cases:
        def handler(request: httpx.Request, error_code=error_code) -> httpx.Response:
            if request.url.host == "ark.cn-beijing.volcengineapi.com":
                return httpx.Response(
                    200,
                    json={"ResponseMetadata": {"Error": {"Code": error_code, "Message": "bad"}}},
                )
            return httpx.Response(404, text=str(request.url))

        invocation, result = invoke(handler)
        assert result is None
        assert invocation.error and invocation.error.code == expected

    def missing_api_key_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ark.cn-beijing.volcengineapi.com":
            return httpx.Response(200, json={"Result": {}})
        return httpx.Response(404, text=str(request.url))

    invocation, result = invoke(missing_api_key_handler)
    assert result is None
    assert invocation.error and invocation.error.code == ErrorCode.provider_auth_failed

    calls = 0
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="seedance-cache.mp4")

    def cached_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if str(request.url) == "https://files.example/cache.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        if request.url.host == "ark.cn-beijing.volcengineapi.com":
            calls += 1
            return httpx.Response(200, json={"Result": {"ApiKey": "tmp-key", "ExpiredTime": 9999999999}})
        if request.url.path == _TASKS:
            return httpx.Response(200, json={"id": f"cgt-cache-{calls}"})
        return httpx.Response(
            200,
            json={
                "id": f"cgt-cache-{calls}",
                "status": "succeeded",
                "content": {"video_url": "https://files.example/cache.mp4"},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(cached_handler))
    secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
    profile = _profile(repository, secret_ref, model_id="ep-seedance")
    for index in range(2):
        gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="video.generate",
                input={"prompt": f"门头特写 {index}"},
            )
        )
    assert calls == 1


def test_sandbox_supports_video_generate():
    result = SandboxProvider().invoke(
        ProviderCall(provider_profile_id="sandbox", capability_id="video.generate", input={"duration_sec": 15})
    )
    assert result.output["video_uri"].startswith("sandbox://video/seedance/")
    assert result.output["video_artifact_id"] is None
    assert result.video_seconds == 15.0
