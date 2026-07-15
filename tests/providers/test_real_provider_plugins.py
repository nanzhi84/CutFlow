from __future__ import annotations

import base64
import httpx
import pytest

from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway
from packages.ai.gateway.provider_gateway import ProviderRuntimeError
from packages.ai.providers import dashscope as dashscope_mod
from packages.ai.providers import minimax as minimax_mod
from packages.ai.providers import openai_image as openai_image_mod
from packages.core.contracts import (
    ErrorCode,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
)
from packages.core.storage.object_store import LocalObjectStore, parse_local_uri
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.assets import store_file
from packages.media.cover_image import COVER_TARGET_HEIGHT, COVER_TARGET_WIDTH


def _gateway(tmp_path, transport: httpx.MockTransport) -> tuple[Repository, ProviderGateway]:
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    object_store = LocalObjectStore(tmp_path / "objects")
    gateway = ProviderGateway(
        repository,
        secret_store=secret_store,
        object_store=object_store,
        http_client=httpx.Client(transport=transport),
    )
    return repository, gateway


def _profile(
    repository: Repository,
    *,
    provider_id: str,
    capability: str,
    model_id: str,
    secret_ref: str,
    default_options: dict | None = None,
) -> ProviderProfile:
    profile = ProviderProfile(
        id=f"{provider_id}.test",
        provider_id=provider_id,
        model_id=model_id,
        capability=capability,
        display_name=f"{provider_id} test",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id=f"provider.{capability}.options"),
        default_options=default_options or {},
    )
    repository.provider_profiles[profile.id] = profile
    return profile


def test_real_plugins_register_alongside_sandbox(tmp_path):
    repository, gateway = _gateway(tmp_path, httpx.MockTransport(lambda request: httpx.Response(500)))

    assert {
        "sandbox",
        "minimax.tts",
        "dashscope.asr",
        "dashscope.vlm",
        "runninghub.heygem",
        "dashscope.llm",
        "dashscope.multimodal_embedding",
        "openai.image",
        "volcengine.seedream",
    } <= set(gateway.plugins)

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id="sandbox.tts.default",
            capability_id="tts.speech",
            input={"text": "sandbox still works"},
        )
    )
    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["audio_uri"].startswith("sandbox://audio/")
    assert repository.provider_profiles["sandbox.tts.default"].provider_id == "sandbox"


def test_minimax_tts_reads_secret_and_stores_real_audio_artifact(tmp_path, media_fixture_factory):
    audio_bytes = media_fixture_factory.audio(duration_sec=1.0).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/t2a_v2"
        assert request.url.params["GroupId"] == "group-1"
        assert request.headers["authorization"] == "Bearer minimax-key"
        payload = httpx.Request("POST", request.url, content=request.content).read()
        assert b"minimax-key" not in payload
        body = __import__("json").loads(payload)
        assert body["model"] == "speech-02-hd"
        assert body["text"] == "hello world"
        assert body["voice_setting"]["voice_id"] == "voice-1"
        return httpx.Response(
            200,
            json={
                "base_resp": {"status_code": 0, "status_msg": "ok"},
                "data": {"audio": audio_bytes.hex(), "duration": 1000},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="minimax.tts",
        capability="tts.speech",
        model_id="speech-02-hd",
        secret_ref=secret_ref,
        default_options={"group_id": "group-1"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"text": "hello world", "voice_id": "voice-1"},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["audio_uri"].startswith("local://")
    artifact = repository.artifacts[result.output["audio_artifact_id"]]
    assert artifact.sha256
    assert artifact.media_info
    assert artifact.media_info.media_type == "audio"
    assert result.input_tokens == len("hello world")
    object_path = gateway.object_store._path(parse_local_uri(result.output["audio_uri"]))  # type: ignore[union-attr]
    assert object_path.read_bytes() == audio_bytes


def test_minimax_tts_subtitle_enabled_returns_asr_shaped_segments(tmp_path, media_fixture_factory):
    audio_bytes = media_fixture_factory.audio(duration_sec=1.0).read_bytes()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/t2a_v2":
            body = __import__("json").loads(request.content)
            # subtitle text is split one sentence per NEWLINE; spoken audio unchanged
            assert body["subtitle_enable"] is True
            assert body["text"] == "第一句。\n第二句。"
            return httpx.Response(
                200,
                json={
                    "base_resp": {"status_code": 0},
                    "data": {
                        "audio": audio_bytes.hex(),
                        "duration": 1000,
                        "subtitle_file": "https://files.example/subtitle.json",
                    },
                },
            )
        if str(request.url) == "https://files.example/subtitle.json":
            # served as octet-stream; provider reads text then json.loads
            return httpx.Response(
                200,
                content=__import__("json")
                .dumps(
                    [
                        {"time_begin": 0, "time_end": 500, "text": "第一句。"},
                        {"time_begin": 500, "time_end": 1000, "text": "第二句。"},
                    ]
                )
                .encode("utf-8"),
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="minimax.tts",
        capability="tts.speech",
        model_id="speech-02-hd",
        secret_ref=secret_ref,
        default_options={"group_id": "group-1"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"text": "第一句。第二句。", "voice_id": "voice-1", "subtitle": True},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["timing"] == {
        "segments": [
            {"start": 0.0, "end": 0.5, "text": "第一句。"},
            {"start": 0.5, "end": 1.0, "text": "第二句。"},
        ],
        "tokens": [],
        "granularity": "segment",
        "text_basis": "original",
    }
    assert requests == ["POST /v1/t2a_v2", "GET /subtitle.json"]


def test_minimax_tts_subtitle_fetch_failure_does_not_break_audio(tmp_path, media_fixture_factory):
    audio_bytes = media_fixture_factory.audio(duration_sec=1.0).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/t2a_v2":
            return httpx.Response(
                200,
                json={
                    "base_resp": {"status_code": 0},
                    "data": {
                        "audio": audio_bytes.hex(),
                        "duration": 1000,
                        "subtitle_file": "https://files.example/subtitle.json",
                    },
                },
            )
        return httpx.Response(500, text="subtitle server down")

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="minimax.tts",
        capability="tts.speech",
        model_id="speech-02-hd",
        secret_ref=secret_ref,
        default_options={"group_id": "group-1"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"text": "第一句。", "voice_id": "voice-1", "subtitle": True},
        )
    )

    # audio synthesis succeeded; subtitle failure is swallowed (no segments)
    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["audio_artifact_id"] in repository.artifacts
    assert "subtitle_segments" not in result.output


def test_videoretalk_submits_async_task_and_stores_polled_video(tmp_path, media_fixture_factory):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="videoretalk-result.mp4")
    result_url = "https://files.example/path/videoretalk-result.mp4?Expires=1&Signature=abc"
    requests: list[str] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v1/services/aigc/image2video/video-synthesis/":
            assert request.method == "POST"
            assert request.headers["x-dashscope-async"] == "enable"
            assert request.headers["authorization"] == "Bearer dashscope-key"
            body = __import__("json").loads(request.content)
            assert body["input"]["video_url"] == "https://media.example/portrait.mp4"
            assert body["input"]["audio_url"] == "https://media.example/speech.wav"
            return httpx.Response(200, json={"output": {"task_id": "vrt-1", "task_status": "PENDING"}})
        if request.url.path == "/api/v1/tasks/vrt-1":
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(200, json={"output": {"task_id": "vrt-1", "task_status": "RUNNING"}})
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "vrt-1",
                        "task_status": "SUCCEEDED",
                        "video_url": result_url,
                    }
                },
            )
        if str(request.url) == result_url:
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.videoretalk",
        capability="lipsync.video",
        model_id="videoretalk",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://dashscope.aliyuncs.com/api/v1",
            "poll_interval": 0,
            "poll_max_attempts": 2,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "video_url": "https://media.example/portrait.mp4",
                "audio_url": "https://media.example/speech.wav",
                "duration_sec": 1.0,
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert invocation.external_job_id == "vrt-1"
    assert result is not None
    assert result.output["external_job_id"] == "vrt-1"
    artifact = repository.artifacts[result.output["video_artifact_id"]]
    assert artifact.uri and artifact.uri.endswith("/videoretalk-result.mp4")
    assert "Expires=" not in artifact.uri
    assert "Signature=" not in artifact.uri
    assert artifact.media_info
    assert artifact.media_info.media_type == "video"
    assert "POST /api/v1/services/aigc/image2video/video-synthesis/" in requests


def test_videoretalk_uses_lipsync_timeout_minutes_as_poll_budget(
    tmp_path, media_fixture_factory, monkeypatch
):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="videoretalk-result.mp4")
    poll_count = 0
    monkeypatch.setattr("packages.ai.providers.dashscope.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path == "/api/v1/services/aigc/image2video/video-synthesis/":
            return httpx.Response(200, json={"output": {"task_id": "vrt-1", "task_status": "PENDING"}})
        if request.url.path == "/api/v1/tasks/vrt-1":
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(200, json={"output": {"task_id": "vrt-1", "task_status": "RUNNING"}})
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "vrt-1",
                        "task_status": "SUCCEEDED",
                        "video_url": "https://files.example/videoretalk-result.mp4",
                    }
                },
            )
        if str(request.url) == "https://files.example/videoretalk-result.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.videoretalk",
        capability="lipsync.video",
        model_id="videoretalk",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://dashscope.aliyuncs.com/api/v1",
            "poll_interval": 1,
            "poll_max_attempts": 1,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "video_url": "https://media.example/portrait.mp4",
                "audio_url": "https://media.example/speech.wav",
                "duration_sec": 1.0,
                "timeout_minutes": 0.05,
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["poll_attempts"] == 2
    assert poll_count == 2


def test_videoretalk_failed_task_surfaces_content_policy_message(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/services/aigc/image2video/video-synthesis/":
            return httpx.Response(200, json={"output": {"task_id": "vrt-9", "task_status": "PENDING"}})
        if request.url.path == "/api/v1/tasks/vrt-9":
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "vrt-9",
                        "task_status": "FAILED",
                        "message": "Input data may contain inappropriate content.",
                    }
                },
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.videoretalk",
        capability="lipsync.video",
        model_id="videoretalk",
        secret_ref=secret_ref,
        default_options={"base_url": "https://dashscope.aliyuncs.com/api/v1", "poll_interval": 0, "poll_max_attempts": 1},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "video_url": "https://media.example/portrait.mp4",
                "audio_url": "https://media.example/speech.wav",
            },
        )
    )

    assert result is None
    assert invocation.error
    assert invocation.error.code == ErrorCode.provider_remote_failed
    assert "inappropriate content" in invocation.error.message.lower()


def test_minimax_tts_http_errors_map_to_spec_codes(tmp_path):
    cases = [
        (httpx.Response(401, text="bad key"), ErrorCode.provider_auth_failed),
        (httpx.Response(429, text="quota"), ErrorCode.provider_quota_exceeded),
        (httpx.Response(500, text="boom"), ErrorCode.provider_remote_failed),
    ]
    for response, expected_code in cases:
        repository, gateway = _gateway(tmp_path, httpx.MockTransport(lambda request, response=response: response))
        secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
        profile = _profile(
            repository,
            provider_id="minimax.tts",
            capability="tts.speech",
            model_id="speech-02-hd",
            secret_ref=secret_ref,
            default_options={"group_id": "group-1"},
        )

        invocation, result = gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="tts.speech",
                input={"text": "hello", "voice_id": "voice-1"},
            )
        )

        assert result is None
        assert invocation.error
        assert invocation.error.code == expected_code

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(timeout_handler))
    secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="minimax.tts",
        capability="tts.speech",
        model_id="speech-02-hd",
        secret_ref=secret_ref,
        default_options={"group_id": "group-1"},
    )
    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"text": "hello", "voice_id": "voice-1"},
        )
    )
    assert result is None
    assert invocation.status == ProviderStatus.timed_out
    assert invocation.error
    assert invocation.error.code == ErrorCode.provider_timeout


def test_minimax_voice_clone_uploads_reference_and_generates_preview(tmp_path, media_fixture_factory):
    reference_audio = media_fixture_factory.audio(duration_sec=1.0, filename="clone-reference.wav")
    preview_audio = media_fixture_factory.audio(duration_sec=1.0, filename="clone-preview.wav").read_bytes()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/files/upload":
            assert request.url.params["GroupId"] == "group-1"
            assert request.headers["authorization"] == "Bearer minimax-key"
            assert b"voice_clone" in request.content
            return httpx.Response(
                200,
                json={"base_resp": {"status_code": 0}, "file": {"file_id": 42}},
            )
        if request.url.path == "/v1/voice_clone":
            body = __import__("json").loads(request.content)
            assert body["model"] == "speech-02-hd"
            assert body["file_id"] == 42
            assert body["voice_id"].startswith("voice_ProviderCl")
            return httpx.Response(200, json={"base_resp": {"status_code": 0}, "voice_id": body["voice_id"]})
        if request.url.path == "/v1/t2a_v2":
            body = __import__("json").loads(request.content)
            assert body["text"] == "试听文本"
            return httpx.Response(
                200,
                json={
                    "base_resp": {"status_code": 0},
                    "data": {"audio": preview_audio.hex(), "duration": 1000},
                },
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    reference = store_file(gateway.object_store, reference_audio, purpose="voice-reference")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="minimax.tts",
        capability="tts.speech",
        model_id="speech-02-hd",
        secret_ref=secret_ref,
        default_options={"group_id": "group-1"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={
                "operation": "clone",
                "display_name": "Provider Clone",
                "reference_audio_uri": reference.ref.uri,
                "preview_text": "试听文本",
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["voice_id"].startswith("voice_ProviderCl")
    assert result.output["preview_audio_artifact_id"] in repository.artifacts
    assert requests == ["/v1/files/upload", "/v1/voice_clone", "/v1/t2a_v2"]


def test_minimax_voice_list_filters_account_voices(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/get_voice"
        assert request.headers["authorization"] == "Bearer minimax-key"
        assert __import__("json").loads(request.content) == {"voice_type": "all"}
        return httpx.Response(
            200,
            json={
                "base_resp": {"status_code": 0},
                "voice_cloning": [
                    {"voice_id": "clone-1", "voice_name": "Boss"},
                    {"voice_id": "clone-2"},
                    {"voice_id": ""},
                    "bad item",
                ],
                "voice_generation": [
                    {"voice_id": "design-1", "voice_name": "Designed"},
                ],
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="minimax.tts",
        capability="tts.speech",
        model_id="speech-02-hd",
        secret_ref=secret_ref,
        default_options={"base_url": "https://api.minimaxi.com/v1"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"operation": "voice_list"},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["voices"] == [
        {"voice_id": "clone-1", "display_name": "Boss", "source": "cloned"},
        {"voice_id": "clone-2", "display_name": "clone-2", "source": "cloned"},
        {"voice_id": "design-1", "display_name": "Designed", "source": "designed"},
    ]


def test_minimax_validation_and_provider_base_resp_errors(tmp_path):
    def profile_for(transport: httpx.MockTransport, *, default_options: dict | None = None):
        repository, gateway = _gateway(tmp_path, transport)
        secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
        profile = _profile(
            repository,
            provider_id="minimax.tts",
            capability="tts.speech",
            model_id="speech-02-hd",
            secret_ref=secret_ref,
            default_options=default_options or {},
        )
        return gateway, profile

    gateway, profile = profile_for(httpx.MockTransport(lambda request: httpx.Response(500)))
    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"operation": "unknown"},
        )
    )
    assert result is None
    assert invocation.error and invocation.error.code == ErrorCode.provider_unsupported_option

    gateway, profile = profile_for(httpx.MockTransport(lambda request: httpx.Response(500)))
    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"text": "hello", "voice_id": "voice-1"},
        )
    )
    assert result is None
    assert invocation.error and invocation.error.message == "MiniMax group_id is required."

    gateway, profile = profile_for(
        httpx.MockTransport(lambda request: httpx.Response(500)),
        default_options={"group_id": "group-1"},
    )
    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"voice_id": "voice-1"},
        )
    )
    assert result is None
    assert invocation.error and invocation.error.message == "Text and voice_id are required."

    cases = [
        (1004, ErrorCode.provider_auth_failed),
        ("1002", ErrorCode.provider_quota_exceeded),
        (1008, ErrorCode.provider_quota_exceeded),
        (7001, ErrorCode.provider_remote_failed),
    ]
    for status_code, expected in cases:
        gateway, profile = profile_for(
            httpx.MockTransport(
                lambda request, status_code=status_code: httpx.Response(
                    200,
                    json={
                        "base_resp": {
                            "status_code": status_code,
                            "status_msg": f"MiniMax {status_code}",
                        }
                    },
                )
            ),
            default_options={"group_id": "group-1"},
        )
        invocation, result = gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="tts.speech",
                input={"text": "hello", "voice_id": "voice-1"},
            )
        )
        assert result is None
        assert invocation.error and invocation.error.code == expected
        assert str(status_code) in invocation.error.message


def test_minimax_audio_and_clone_validation_edges(tmp_path):
    def invoke_with(handler, input_payload, *, default_options: dict | None = None):
        repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
        secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
        profile = _profile(
            repository,
            provider_id="minimax.tts",
            capability="tts.speech",
            model_id="speech-02-hd",
            secret_ref=secret_ref,
            default_options=default_options or {"group_id": "group-1"},
        )
        return gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="tts.speech",
                input=input_payload,
            )
        )

    invocation, result = invoke_with(
        lambda request: httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {}}),
        {"text": "hello", "voice_id": "voice-1"},
    )
    assert result is None
    assert invocation.error and invocation.error.message == "MiniMax TTS response missing audio."

    invocation, result = invoke_with(
        lambda request: httpx.Response(
            200, json={"base_resp": {"status_code": 0}, "data": {"audio": "not-hex"}}
        ),
        {"text": "hello", "voice_id": "voice-1"},
    )
    assert result is None
    assert invocation.error and invocation.error.message == "MiniMax TTS audio is invalid."

    invocation, result = invoke_with(
        lambda request: httpx.Response(500),
        {"operation": "clone"},
    )
    assert result is None
    assert invocation.error and invocation.error.message == "Reference audio is required."

    reference_path = tmp_path / "too-large.wav"
    reference_path.write_bytes(b"larger than one byte")
    repository, gateway = _gateway(tmp_path, httpx.MockTransport(lambda request: httpx.Response(500)))
    stored_ref = store_file(gateway.object_store, reference_path, purpose="voice-reference")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("minimax-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="minimax.tts",
        capability="tts.speech",
        model_id="speech-02-hd",
        secret_ref=secret_ref,
        default_options={"group_id": "group-1", "clone_max_bytes": 1},
    )
    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"operation": "clone", "reference_audio_uri": stored_ref.ref.uri},
        )
    )
    assert result is None
    assert invocation.error and invocation.error.message == "Reference audio file is too large."


def test_minimax_helpers_strip_audio_and_generate_safe_voice_ids(monkeypatch):
    monkeypatch.setattr(minimax_mod.time, "time", lambda: 1234567890)

    assert minimax_mod._usage_safe({"data": {"audio": "hex-secret", "duration": 1000}}) == {
        "duration": 1000
    }
    assert minimax_mod._usage_safe({"data": "not a dict"}) == {}
    assert minimax_mod._generate_voice_id("123 bad name", prefix="clone") == "clone_v123badnam_1234567890"
    generated = minimax_mod._generate_voice_id("中文名", prefix="voice")
    assert generated.startswith("voice_")
    assert generated.endswith("_1234567890")


def test_dashscope_asr_uses_async_transcription_task_and_downloads_alignment(tmp_path):
    requests: list[str] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v1/services/audio/asr/transcription":
            assert request.method == "POST"
            assert request.headers["authorization"] == "Bearer dashscope-key"
            assert request.headers["x-dashscope-async"] == "enable"
            body = __import__("json").loads(request.content)
            assert body == {
                "model": "paraformer-v2",
                "input": {"file_urls": ["https://media.example/speech.wav"]},
                "parameters": {
                    "language_hints": ["zh"],
                    "timestamp_alignment_enabled": True,
                },
            }
            return httpx.Response(200, json={"output": {"task_id": "asr-task-1", "task_status": "PENDING"}})
        if request.url.path == "/api/v1/tasks/asr-task-1":
            assert request.method == "GET"
            assert request.headers["authorization"] == "Bearer dashscope-key"
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(200, json={"output": {"task_id": "asr-task-1", "task_status": "RUNNING"}})
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "asr-task-1",
                        "task_status": "SUCCEEDED",
                        "results": [{"transcription_url": "https://files.example/asr-result.json"}],
                    },
                    "usage": {"duration": 2100},
                },
            )
        if str(request.url) == "https://files.example/asr-result.json":
            assert request.method == "GET"
            return httpx.Response(
                200,
                json=[
                    {
                        "file_url": "https://media.example/speech.wav",
                        "transcripts": [
                            {
                                "channel_id": 0,
                                "content": "你好世界。",
                                "sentences": [
                                    {"begin_time": 0, "end_time": 900, "text": "你好"},
                                    {"begin_time": 900, "end_time": 2100, "text": "世界。"},
                                ],
                            }
                        ],
                    }
                ],
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.asr",
        capability="asr.transcribe",
        model_id="paraformer-v2",
        secret_ref=secret_ref,
        default_options={"poll_interval": 0, "poll_max_attempts": 2},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="asr.transcribe",
            input={"audio_uri": "https://media.example/speech.wav", "language_hints": ["zh"]},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert invocation.external_job_id == "asr-task-1"
    assert result is not None
    assert result.output == {
        "text": "你好世界。",
        "segments": [
            {"start": 0.0, "end": 0.9, "text": "你好"},
            {"start": 0.9, "end": 2.1, "text": "世界。"},
        ],
        "timing": {
            "segments": [
                {"start": 0.0, "end": 0.9, "text": "你好"},
                {"start": 0.9, "end": 2.1, "text": "世界。"},
            ],
            "tokens": [],
            "granularity": "segment",
            "text_basis": "normalized",
        },
        "source": "asr",
    }
    assert result.audio_seconds == 2.1
    assert requests == [
        "POST /api/v1/services/audio/asr/transcription",
        "GET /api/v1/tasks/asr-task-1",
        "GET /api/v1/tasks/asr-task-1",
        "GET /asr-result.json",
    ]


def test_dashscope_llm_uses_compatible_chat_base_url_and_ignores_legacy_max_tokens(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer dashscope-key"
        body = __import__("json").loads(request.content)
        # ``max_tokens`` used to be pinned on seeded DashScope profiles; the
        # unbounded qwen3.7 path must not forward that stale limiter.
        assert "max_tokens" not in body
        assert body == {
            "model": "qwen3.7-plus",
            "messages": [{"role": "user", "content": "Return JSON."}],
            "temperature": 0.7,
            "response_format": {"type": "json_object"},
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.llm",
        capability="llm.chat",
        model_id="qwen3.7-plus",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "temperature": 0.7,
            "max_tokens": 2000,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="llm.chat",
            input={
                "messages": [{"role": "user", "content": "Return JSON."}],
                "response_format": {"type": "json_object"},
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["intent"] == {"ok": True}
    assert result.input_tokens == 11
    assert result.output_tokens == 7


def test_dashscope_multimodal_embedding_uses_native_multimodal_embedding_url(tmp_path):
    embedding = [0.001] * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == (
            "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
        )
        assert request.headers["authorization"] == "Bearer dashscope-key"
        body = __import__("json").loads(request.content)
        assert body == {
            "model": "qwen3-vl-embedding",
            "input": {"contents": [{"text": "施工前现场细节"}]},
            "parameters": {"dimension": 1024},
        }
        return httpx.Response(
            200,
            json={
                "request_id": "emb_req_1",
                "output": {"embeddings": [{"index": 0, "embedding": embedding, "type": "vl"}]},
                "usage": {"input_tokens": 7, "total_tokens": 7},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.multimodal_embedding",
        capability="multimodal.embedding",
        model_id="qwen3-vl-embedding",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "dimension": 1024,
            "normalization": "l2",
            "index_version": "clip-video-qwen3-v3",
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="multimodal.embedding",
            input={"retrieval_intent": "施工前现场细节"},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["embedding"] == embedding
    assert result.output["dimension"] == 1024
    assert result.output["model"] == "qwen3-vl-embedding"


def test_dashscope_multimodal_embedding_accepts_video_url(tmp_path):
    embedding = [0.002] * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body == {
            "model": "qwen3-vl-embedding",
            "input": {"contents": [{"video": "https://oss.example/clip.mp4"}]},
            "parameters": {
                "dimension": 1024,
                "instruct": "video_clip_retrieval_v1",
            },
        }
        return httpx.Response(
            200,
            json={
                "request_id": "emb_req_video_1",
                "output": {"embeddings": [{"index": 0, "embedding": embedding, "type": "vl"}]},
                "usage": {"input_tokens": 0, "image_tokens": 128, "total_tokens": 128},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.multimodal_embedding",
        capability="multimodal.embedding",
        model_id="qwen3-vl-embedding",
        secret_ref=secret_ref,
        default_options={"dimension": 1024},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="multimodal.embedding",
            input={
                "video_url": "https://oss.example/clip.mp4",
                "instruct": "video_clip_retrieval_v1",
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["embedding"] == embedding
    assert result.output["index_version"] == "clip-video-qwen3-v3"
    assert result.output["normalization"] == "l2"
    assert result.input_tokens == 128


def test_dashscope_llm_invalid_json_returns_text_intent(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "plain non-json answer"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.llm",
        capability="llm.chat",
        model_id="qwen3.7-plus",
        secret_ref=secret_ref,
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="llm.chat",
            input={"messages": [{"role": "user", "content": "Return JSON."}]},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output == {
        "content": "plain non-json answer",
        "intent": {"text": "plain non-json answer"},
    }
    assert result.input_tokens == 5
    assert result.output_tokens == 4


def test_dashscope_vlm_uses_openai_compatible_multimodal_payload(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer dashscope-key"
        body = __import__("json").loads(request.content)
        assert "max_tokens" not in body
        assert body == {
            "model": "qwen-vl-max-latest",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Return canonical JSON."},
                        {"type": "image_url", "image_url": {"url": "https://media.example/frame.jpg"}},
                    ],
                }
            ],
            "temperature": 0.2,
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"labels": ["broll"], "quality": {"valid": true, "issues": []}}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 21, "completion_tokens": 13},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.vlm",
        capability="vlm.annotation",
        model_id="qwen-vl-max-latest",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "temperature": 0.2,
            "max_tokens": 1200,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="vlm.annotation",
            input={
                "asset_kind": "image",
                "asset_uri": "https://media.example/frame.jpg",
                "prompt": "Return canonical JSON.",
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["canonical"]["quality"]["valid"] is True
    assert result.output["content"] == '{"labels": ["broll"], "quality": {"valid": true, "issues": []}}'
    assert result.input_tokens == 21
    assert result.output_tokens == 13


def test_dashscope_vlm_preserves_unparseable_content(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"segments": [{"start": 0.0}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1200},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.vlm",
        capability="vlm.annotation",
        model_id="qwen-vl-max-latest",
        secret_ref=secret_ref,
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="vlm.annotation",
            input={"messages": [{"role": "user", "content": [{"type": "text", "text": "Return JSON."}]}]},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["canonical"] == {}
    assert result.output["content"] == '{"segments": [{"start": 0.0}'


def test_dashscope_asr_rejects_missing_audio_uri(tmp_path):
    repository, gateway = _gateway(tmp_path, httpx.MockTransport(lambda request: httpx.Response(500)))
    secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.asr",
        capability="asr.transcribe",
        model_id="paraformer-v2",
        secret_ref=secret_ref,
    )

    invocation, result = gateway.invoke(
        ProviderCall(provider_profile_id=profile.id, capability_id="asr.transcribe", input={})
    )

    assert result is None
    assert invocation.error
    assert invocation.error.code == ErrorCode.provider_unsupported_option
    assert "audio_uri is required" in invocation.error.message


def test_dashscope_asr_submit_and_task_output_must_include_required_urls(tmp_path):
    cases = [
        (
            lambda request: httpx.Response(200, json={"output": {"task_status": "PENDING"}}),
            "missing task ID",
        ),
        (
            lambda request: httpx.Response(
                200,
                json=(
                    {"output": {"task_id": "asr-1", "task_status": "PENDING"}}
                    if request.url.path == "/api/v1/services/audio/asr/transcription"
                    else {"output": {"task_id": "asr-1", "task_status": "SUCCEEDED"}}
                ),
            ),
            "missing transcription_url",
        ),
    ]

    for handler, expected_message in cases:
        repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
        secret_ref = gateway.secret_store.put("dashscope-key")  # type: ignore[union-attr]
        profile = _profile(
            repository,
            provider_id="dashscope.asr",
            capability="asr.transcribe",
            model_id="paraformer-v2",
            secret_ref=secret_ref,
            default_options={"poll_interval": 0, "poll_max_attempts": 1},
        )

        invocation, result = gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="asr.transcribe",
                input={"audio_uri": "https://media.example/speech.wav"},
            )
        )

        assert result is None
        assert invocation.error
        assert invocation.error.code == ErrorCode.provider_remote_failed
        assert expected_message in invocation.error.message


def test_dashscope_task_polling_failure_timeout_and_helper_parsing(monkeypatch):
    monkeypatch.setattr(dashscope_mod.time, "sleep", lambda _seconds: None)
    failed_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"output": {"task_status": "FAILED", "message": "policy"}},
            )
        )
    )
    with pytest.raises(ProviderRuntimeError) as failed_exc:
        dashscope_mod.poll_dashscope_task(
            client=failed_client,
            base_url="https://dashscope.aliyuncs.com/api/v1",
            api_key="key",
            task_id="task-1",
            options={"poll_interval": 0, "poll_max_attempts": 1},
            timeout_sec=30,
        )
    assert failed_exc.value.code == ErrorCode.provider_remote_failed
    assert "policy" in failed_exc.value.message

    timeout_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"output": {"taskStatus": "RUNNING"}})
        )
    )
    with pytest.raises(ProviderRuntimeError) as timeout_exc:
        dashscope_mod.poll_dashscope_task(
            client=timeout_client,
            base_url="https://dashscope.aliyuncs.com/api/v1",
            api_key="key",
            task_id="task-2",
            options={"poll_interval": 0, "poll_max_attempts": 1},
            timeout_sec=30,
        )
    assert timeout_exc.value.code == ErrorCode.provider_timeout

    assert dashscope_mod.task_id_from_payload({"id": "root-id"}) == "root-id"
    assert dashscope_mod._task_status({"state": "FINISH"}) == "finish"
    assert (
        dashscope_mod._find_transcription_url({"nested": [{"transcriptionUrl": "https://x/asr.json"}]})
        == "https://x/asr.json"
    )
    assert dashscope_mod._find_transcription_url("local/path.json") is None
    assert dashscope_mod._duration_from_task_payload({"usage": {"duration": 1200}}) == 1.2
    assert dashscope_mod._duration_from_task_payload({"usage": {"duration": 12}}) == 12
    assert dashscope_mod._duration_from_task_payload({"usage": {"duration": ""}}) == 0
    assert dashscope_mod._chat_url({"chat_completions_url": "https://x/chat"}) == "https://x/chat"
    assert (
        dashscope_mod._chat_url({"base_url": "https://x/compatible-mode/v1/chat/completions"})
        == "https://x/compatible-mode/v1/chat/completions"
    )
    assert dashscope_mod._parse_json_object("```json\n{\"ok\": true}\n```") == {"ok": True}
    assert dashscope_mod._parse_json_object("[1, 2]") == {}
    assert dashscope_mod._message_content({"content": "fallback"}) == "fallback"
    assert dashscope_mod._message_content({"text": "text fallback"}) == "text fallback"
    assert dashscope_mod._usage({"usage": None}, "prompt_tokens") == 0


def test_dashscope_task_polling_retries_transient_transport_failure(monkeypatch):
    monkeypatch.setattr(dashscope_mod.time, "sleep", lambda _seconds: None)
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError(
                "[SSL: UNEXPECTED_EOF_WHILE_READING]",
                request=request,
            )
        return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED"}})

    payload, attempts = dashscope_mod.poll_dashscope_task(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="key",
        task_id="task-transient",
        options={"poll_interval": 0, "poll_max_attempts": 2},
        timeout_sec=30,
    )

    assert attempts == 2
    assert requests == 2
    assert dashscope_mod._task_status(payload) == "succeeded"


def test_dashscope_task_polling_bounds_consecutive_transport_failures(monkeypatch):
    monkeypatch.setattr(dashscope_mod.time, "sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    with pytest.raises(ProviderRuntimeError) as exc_info:
        dashscope_mod.poll_dashscope_task(
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            base_url="https://dashscope.aliyuncs.com/api/v1",
            api_key="key",
            task_id="task-offline",
            options={
                "poll_interval": 0,
                "poll_max_attempts": 10,
                "poll_max_consecutive_errors": 3,
            },
            timeout_sec=30,
        )

    assert exc_info.value.code == ErrorCode.provider_remote_failed
    assert "3 consecutive times" in exc_info.value.message


def test_dashscope_transcription_payloads_cover_words_and_fallbacks():
    text, segments = dashscope_mod._parse_transcription_payload(
        {
            "transcripts": {
                "sentences": [
                    {
                        "words": [
                            {"text": "你", "begin_time": 0, "end_time": 200},
                            {"text": "好", "begin_time": 200, "end_time": 400, "punctuation": "。"},
                            {"text": "", "punctuation": ""},
                            "bad",
                        ]
                    },
                    {"text": "尾句", "start": 0.4, "end": 0.3},
                ],
                "content": "你好。尾句",
            }
        }
    )

    assert text == "你好。尾句"
    assert segments[0] == {"start": 0.0, "end": 0.4, "text": "你好。"}
    assert segments[1]["text"] == "尾句"
    assert segments[1]["end"] > segments[1]["start"]

    fallback_text, fallback_segments = dashscope_mod._parse_transcription_payload(
        {"content": "只有全文"}
    )
    assert fallback_text == "只有全文"
    assert fallback_segments == [{"start": 0.0, "end": 0.0, "text": "只有全文"}]
    assert dashscope_mod._transcripts_from_payload(["bad", {"sentence": []}]) == [{"sentence": []}]


def test_runninghub_heygem_records_external_job_and_stores_polled_video(
    tmp_path, media_fixture_factory
):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="heygem-result.mp4")
    result_url = "https://files.example/path/heygem-result.mp4?Expires=1&Signature=abc"
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/openapi/v2/media/upload/binary":
            if requests.count("POST /openapi/v2/media/upload/binary") == 1:
                return httpx.Response(200, json={"data": {"fileName": "portrait.mp4"}})
            return httpx.Response(200, json={"data": {"fileName": "speech.wav"}})
        if request.url.path == "/task/openapi/ai-app/run":
            return httpx.Response(200, json={"data": {"taskId": "rh-job-1"}})
        if request.url.path == "/task/openapi/status":
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.url.path == "/task/openapi/outputs":
            return httpx.Response(
                200,
                json={"data": {"fileUrl": result_url, "consumeCoins": 3}},
            )
        if str(request.url) == result_url:
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "poll_interval": 0,
            "poll_max_attempts": 1,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert invocation.external_job_id == "rh-job-1"
    assert result is not None
    assert result.provider_credits == 3
    assert result.output["video_uri"].startswith("local://")
    artifact = repository.artifacts[result.output["video_artifact_id"]]
    assert artifact.uri and artifact.uri.endswith("/heygem-result.mp4")
    assert "Expires=" not in artifact.uri
    assert "Signature=" not in artifact.uri
    assert artifact.media_info
    assert artifact.media_info.media_type == "video"
    assert "POST /task/openapi/status" in requests
    assert "POST /task/openapi/outputs" in requests


def test_runninghub_heygem_uses_lipsync_timeout_minutes_as_poll_budget(
    tmp_path, media_fixture_factory, monkeypatch
):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="heygem-result.mp4")
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    upload_calls = 0
    status_calls = 0
    monkeypatch.setattr("packages.ai.providers.runninghub.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_calls, status_calls
        if request.url.path == "/openapi/v2/media/upload/binary":
            upload_calls += 1
            filename = "portrait.mp4" if upload_calls == 1 else "speech.wav"
            return httpx.Response(200, json={"data": {"fileName": filename}})
        if request.url.path == "/task/openapi/ai-app/run":
            return httpx.Response(200, json={"data": {"taskId": "rh-job-1"}})
        if request.url.path == "/task/openapi/status":
            status_calls += 1
            if status_calls == 1:
                return httpx.Response(200, json={"data": {"status": "running"}})
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.url.path == "/task/openapi/outputs":
            return httpx.Response(200, json={"data": {"fileUrl": "https://files.example/heygem-result.mp4"}})
        if str(request.url) == "https://files.example/heygem-result.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "poll_interval": 1,
            "poll_max_attempts": 1,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
                "timeout_minutes": 0.05,
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["poll_attempts"] == 2
    assert status_calls == 2


def test_runninghub_heygem_discovers_node_mapping_when_not_configured(
    tmp_path, media_fixture_factory
):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="heygem-result.mp4")
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/openapi/v2/media/upload/binary":
            if requests.count("POST /openapi/v2/media/upload/binary") == 1:
                return httpx.Response(200, json={"data": {"fileName": "portrait.mp4"}})
            return httpx.Response(200, json={"data": {"fileName": "speech.wav"}})
        if request.url.path == "/api/webapp/apiCallDemo":
            assert request.method == "GET"
            assert request.url.params["apiKey"] == "runninghub-key"
            assert request.url.params["webappId"] == "webapp-1"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "nodes": [
                            {"nodeId": "video-node", "fieldName": "video", "fieldType": "video"},
                            {"nodeId": "audio-node", "fieldName": "audio", "fieldType": "audio"},
                        ]
                    }
                },
            )
        if request.url.path == "/task/openapi/ai-app/run":
            body = __import__("json").loads(request.content)
            assert body["nodeInfoList"] == [
                {"nodeId": "video-node", "fieldName": "video", "fieldValue": "portrait.mp4"},
                {"nodeId": "audio-node", "fieldName": "audio", "fieldValue": "speech.wav"},
            ]
            return httpx.Response(200, json={"data": {"taskId": "rh-job-1"}})
        if request.url.path == "/task/openapi/status":
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.url.path == "/task/openapi/outputs":
            return httpx.Response(200, json={"data": {"fileUrl": "https://files.example/heygem-result.mp4"}})
        if str(request.url) == "https://files.example/heygem-result.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "poll_interval": 0,
            "poll_max_attempts": 1,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["external_job_id"] == "rh-job-1"
    assert "GET /api/webapp/apiCallDemo" in requests


def test_runninghub_heygem_retries_status_disconnect(tmp_path, media_fixture_factory):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="heygem-result.mp4")
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.url.path == "/openapi/v2/media/upload/binary":
            return httpx.Response(200, json={"data": {"fileName": f"{request.url.params.get('kind', 'media')}.bin"}})
        if request.url.path == "/task/openapi/ai-app/run":
            return httpx.Response(200, json={"data": {"taskId": "rh-job-1"}})
        if request.url.path == "/task/openapi/status":
            status_calls += 1
            if status_calls == 1:
                raise httpx.TransportError("server disconnected", request=request)
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.url.path == "/task/openapi/outputs":
            return httpx.Response(200, json={"data": {"fileUrl": "https://files.example/heygem-result.mp4"}})
        if str(request.url) == "https://files.example/heygem-result.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "poll_interval": 0,
            "poll_max_attempts": 1,
            "retry_base_delay": 0,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert status_calls == 2


def test_runninghub_heygem_retries_upload_disconnect(tmp_path, media_fixture_factory):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="heygem-result.mp4")
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    upload_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_calls
        if request.url.path == "/openapi/v2/media/upload/binary":
            upload_calls += 1
            if upload_calls == 1:
                raise httpx.TransportError("server disconnected", request=request)
            if upload_calls == 2:
                return httpx.Response(200, json={"data": {"fileName": "portrait.mp4"}})
            return httpx.Response(200, json={"data": {"fileName": "speech.wav"}})
        if request.url.path == "/task/openapi/ai-app/run":
            return httpx.Response(200, json={"data": {"taskId": "rh-job-1"}})
        if request.url.path == "/task/openapi/status":
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.url.path == "/task/openapi/outputs":
            return httpx.Response(200, json={"data": {"fileUrl": "https://files.example/heygem-result.mp4"}})
        if str(request.url) == "https://files.example/heygem-result.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "poll_interval": 0,
            "poll_max_attempts": 1,
            "retry_base_delay": 0,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert upload_calls == 3


def test_runninghub_heygem_failed_status_reports_task_id(tmp_path, media_fixture_factory):
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi/v2/media/upload/binary":
            return httpx.Response(200, json={"data": {"fileName": "input.bin"}})
        if request.url.path == "/task/openapi/ai-app/run":
            return httpx.Response(200, json={"data": {"taskId": "rh-job-123"}})
        if request.url.path == "/task/openapi/status":
            return httpx.Response(200, json={"code": 0, "msg": "success", "data": "FAILED"})
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "poll_interval": 0,
            "poll_max_attempts": 1,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert result is None
    assert invocation.error
    assert "rh-job-123" in invocation.error.message
    assert "FAILED" in invocation.error.message


def test_runninghub_heygem_submit_surfaces_queue_limit_and_retries(tmp_path, media_fixture_factory):
    """RunningHub returns HTTP 200 with an application-level code on capacity
    limits (415/421). The old code dropped `data: null` to the misleading
    "missing task ID"; instead surface the real msg and retry the transient
    capacity condition before giving up."""
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    run_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal run_calls
        if request.url.path == "/openapi/v2/media/upload/binary":
            return httpx.Response(200, json={"data": {"fileName": "input.bin"}})
        if request.url.path == "/task/openapi/ai-app/run":
            run_calls += 1
            return httpx.Response(
                200, json={"code": 421, "msg": "TASK_QUEUE_MAXED", "data": None}
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "retry_base_delay": 0,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert result is None
    assert invocation.status == ProviderStatus.failed
    assert invocation.error
    # Real reason surfaced, not the misleading generic message.
    assert "TASK_QUEUE_MAXED" in invocation.error.message
    assert "421" in invocation.error.message
    assert "missing task ID" not in invocation.error.message
    # Transient capacity limit is retried before giving up.
    assert run_calls == 3


def test_runninghub_heygem_submit_does_not_retry_transport_error(tmp_path, media_fixture_factory):
    """A transport error (timeout / reset / network 5xx) on the task-CREATING
    /run endpoint must NOT be retried: /run carries no idempotency key, so a
    request that already created (and billed) a task server-side before its
    response was lost would be duplicated by a retry. Only explicit capacity
    codes (415/421), which reject before task creation, are retried. Upload/poll
    still retry transport errors — only the billable /run submit is exempt."""
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    run_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal run_calls
        if request.url.path == "/openapi/v2/media/upload/binary":
            return httpx.Response(200, json={"data": {"fileName": "input.bin"}})
        if request.url.path == "/task/openapi/ai-app/run":
            run_calls += 1
            raise httpx.TimeoutException("slow", request=request)
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "retry_base_delay": 0,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert result is None
    assert invocation.status == ProviderStatus.timed_out
    # The non-idempotent create is sent exactly once on a transport error.
    assert run_calls == 1


def test_runninghub_heygem_submit_surfaces_insufficient_balance_without_retry(
    tmp_path, media_fixture_factory
):
    """Balance exhaustion (416) is a hard failure: surface it as a quota error
    and do NOT waste retries on it."""
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    run_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal run_calls
        if request.url.path == "/openapi/v2/media/upload/binary":
            return httpx.Response(200, json={"data": {"fileName": "input.bin"}})
        if request.url.path == "/task/openapi/ai-app/run":
            run_calls += 1
            return httpx.Response(
                200,
                json={
                    "code": 416,
                    "msg": "TASK_CREATE_FAILED_BY_NOT_ENOUGH_WALLET",
                    "data": None,
                },
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "retry_base_delay": 0,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert result is None
    assert invocation.error
    assert invocation.error.code == ErrorCode.provider_quota_exceeded
    assert "416" in invocation.error.message
    assert run_calls == 1


def test_runninghub_heygem_submit_surfaces_invalid_api_key(tmp_path, media_fixture_factory):
    """An invalid API key (802) is surfaced as an auth failure, not the generic
    "missing task ID", and is not retried."""
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    run_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal run_calls
        if request.url.path == "/openapi/v2/media/upload/binary":
            return httpx.Response(200, json={"data": {"fileName": "input.bin"}})
        if request.url.path == "/task/openapi/ai-app/run":
            run_calls += 1
            return httpx.Response(
                200, json={"code": 802, "msg": "APIKEY_UNAUTHORIZED", "data": None}
            )
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "retry_base_delay": 0,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert result is None
    assert invocation.error
    assert invocation.error.code == ErrorCode.provider_auth_failed
    assert "APIKEY_UNAUTHORIZED" in invocation.error.message
    assert run_calls == 1


def test_runninghub_heygem_submit_does_not_retry_unrecognised_rejection(
    tmp_path, media_fixture_factory
):
    """An unrecognised non-zero code is surfaced verbatim but treated as
    terminal — only the known transient capacity codes get retried."""
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")
    run_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal run_calls
        if request.url.path == "/openapi/v2/media/upload/binary":
            return httpx.Response(200, json={"data": {"fileName": "input.bin"}})
        if request.url.path == "/task/openapi/ai-app/run":
            run_calls += 1
            return httpx.Response(200, json={"code": 804, "msg": "APIKEY_TASK_IS_RUNNING", "data": None})
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "retry_base_delay": 0,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert result is None
    assert invocation.error
    assert "APIKEY_TASK_IS_RUNNING" in invocation.error.message
    assert "804" in invocation.error.message
    assert run_calls == 1


def test_runninghub_heygem_submit_accepts_explicit_success_envelope(tmp_path, media_fixture_factory):
    """The real RunningHub success envelope carries `code: 0` alongside the
    taskId; the success path must keep working with the code present."""
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="heygem-result.mp4")
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi/v2/media/upload/binary":
            return httpx.Response(200, json={"data": {"fileName": "input.bin"}})
        if request.url.path == "/task/openapi/ai-app/run":
            return httpx.Response(
                200, json={"code": 0, "msg": "success", "data": {"taskId": "rh-job-1"}}
            )
        if request.url.path == "/task/openapi/status":
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.url.path == "/task/openapi/outputs":
            return httpx.Response(
                200, json={"data": {"fileUrl": "https://files.example/heygem-result.mp4"}}
            )
        if str(request.url) == "https://files.example/heygem-result.mp4":
            return httpx.Response(200, content=result_video.read_bytes())
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    video_stored = store_file(gateway.object_store, source_video, purpose="test-video")  # type: ignore[arg-type]
    audio_stored = store_file(gateway.object_store, source_audio, purpose="test-audio")  # type: ignore[arg-type]
    secret_ref = gateway.secret_store.put("runninghub-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "poll_interval": 0,
            "poll_max_attempts": 1,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": video_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 1.0,
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert invocation.external_job_id == "rh-job-1"


_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f"
    "15c4890000000b49444154789c6360000200000500017a5eab3f00000000"
    "49454e44ae426082"
)


def _assert_cover_artifact_is_9_16(repository: Repository, artifact_id: str) -> None:
    artifact = repository.artifacts[artifact_id]
    assert artifact.kind.value == "cover.image"
    assert artifact.media_info
    assert artifact.media_info.media_type == "image"
    assert artifact.media_info.width == COVER_TARGET_WIDTH
    assert artifact.media_info.height == COVER_TARGET_HEIGHT


def test_openai_image_generates_cover_from_b64_json(tmp_path):
    import base64
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer image-key"
        body = json.loads(request.content)
        assert body["model"] == "gpt-image-2-all"
        assert "封面测试" in body["prompt"]
        # neuromash mirror -> only size/n forwarded (faithful to origin filter).
        assert set(body) == {"model", "prompt", "size", "n"}
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(_PNG_1x1).decode("ascii")}],
                "usage": {"input_tokens": 12},
            },
        )

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("image-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="openai.image",
        capability="image.generate",
        model_id="gpt-image-2-all",
        secret_ref=secret_ref,
        default_options={"base_url": "https://example.invalid/v1", "provider_kind": "neuromash", "size": "1024x1536"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="image.generate",
            input={"prompt": "封面测试 cover prompt"},
            idempotency_key="cover-run-1",
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.image_count == 1
    _assert_cover_artifact_is_9_16(repository, result.output["cover_artifact_id"])


def test_volcengine_seedream_uses_openai_compatible_image_payload(tmp_path):
    import json

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v3/images/generations":
            assert request.method == "POST"
            assert request.headers["authorization"] == "Bearer ark-key"
            body = json.loads(request.content)
            assert body == {
                "model": "doubao-seedream-5-0-260128",
                "prompt": "封面测试 cover prompt",
                "size": "2K",
                "n": 1,
                "response_format": "url",
                "watermark": False,
            }
            assert "image" not in body
            return httpx.Response(200, json={"data": [{"url": "https://cdn.invalid/seedream.png"}]})
        assert str(request.url) == "https://cdn.invalid/seedream.png"
        return httpx.Response(200, content=_PNG_1x1)

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("ark-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="volcengine.seedream",
        capability="image.generate",
        model_id="doubao-seedream-5-0-260128",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "size": "2K",
            "response_format": "url",
            "watermark": False,
            "n": 1,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="image.generate",
            input={"prompt": "封面测试 cover prompt"},
            idempotency_key="cover-seedream-1",
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert requests == ["POST /api/v3/images/generations", "GET /seedream.png"]
    _assert_cover_artifact_is_9_16(repository, result.output["cover_artifact_id"])


def test_volcengine_seedream_sends_reference_image_on_generation_endpoint(tmp_path):
    import base64
    import json

    requests: list[str] = []
    reference_b64 = base64.b64encode(_PNG_1x1).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v3/images/generations":
            assert request.method == "POST"
            assert request.headers["authorization"] == "Bearer ark-key"
            body = json.loads(request.content)
            assert body["model"] == "doubao-seedream-5-0-260128"
            assert body["prompt"] == "封面测试 cover prompt"
            assert body["size"] == "2K"
            assert body["response_format"] == "url"
            assert body["image"].startswith("data:image/png;base64,")
            assert body["image"].split(",", 1)[1] == reference_b64
            return httpx.Response(200, json={"data": [{"url": "https://cdn.invalid/seedream-ref.png"}]})
        if str(request.url) == "https://cdn.invalid/seedream-ref.png":
            return httpx.Response(200, content=_PNG_1x1)
        raise AssertionError(f"unexpected request to {request.url}")

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("ark-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="volcengine.seedream",
        capability="image.generate",
        model_id="doubao-seedream-5-0-260128",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "size": "2K",
            "response_format": "url",
            "watermark": False,
            "n": 1,
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="image.generate",
            input={
                "prompt": "封面测试 cover prompt",
                "reference_image_b64": reference_b64,
                "reference_filename": "source-frame.png",
            },
            idempotency_key="cover-seedream-ref-1",
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["reference_image_requested"] is True
    assert result.output["reference_image_used"] is True
    assert result.output["reference_transport"] == "images.generations.image"
    assert invocation.request_artifact_id in repository.artifacts
    request_artifact = repository.artifacts[invocation.request_artifact_id]
    assert request_artifact.kind.value == "provider.raw_request"
    request_path = gateway.object_store._path(parse_local_uri(request_artifact.uri))  # type: ignore[union-attr]
    assert request_path.read_bytes() == _PNG_1x1
    assert requests == ["POST /api/v3/images/generations", "GET /seedream-ref.png"]
    _assert_cover_artifact_is_9_16(repository, result.output["cover_artifact_id"])


def test_volcengine_seedream_exchanges_ak_sk_for_temporary_api_key(tmp_path):
    import json

    requests: list[str] = []
    openapi_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.host}{request.url.path}")
        auth = request.headers.get("authorization", "")
        if request.url.host == "ark.cn-beijing.volcengineapi.com":
            assert request.url.path == "/"
            assert str(request.url.query, "utf-8") == "Action=GetApiKey&Version=2024-01-01"
            assert auth.startswith("HMAC-SHA256 Credential=AKLTxxx/")
            assert "/cn-beijing/ark/request" in auth
            assert "sk-secret" not in auth
            payload = json.loads(request.content)
            openapi_bodies.append(payload)
            assert payload["DurationSeconds"] == 604800
            assert payload["ResourceType"] == "presetendpoint"
            assert payload["ResourceIds"] == ["doubao-seedream-5-0-260128"]
            assert payload["ProjectName"] == "default"
            return httpx.Response(
                200,
                json={
                    "ResponseMetadata": {"Action": "GetApiKey"},
                    "Result": {"ApiKey": "tmp-seedream-key", "ExpiredTime": 0},
                },
            )
        if request.url.path == "/api/v3/images/generations":
            assert auth == "Bearer tmp-seedream-key"
            body = json.loads(request.content)
            assert body == {
                "model": "doubao-seedream-5-0-260128",
                "prompt": "封面测试 cover prompt",
                "size": "2K",
                "n": 1,
                "response_format": "url",
                "watermark": False,
            }
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.invalid/seedream-ak.png"}]},
            )
        if str(request.url) == "https://cdn.invalid/seedream-ak.png":
            assert auth == "Bearer tmp-seedream-key"
            return httpx.Response(200, content=_PNG_1x1)
        return httpx.Response(404, text=str(request.url))

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="volcengine.seedream",
        capability="image.generate",
        model_id="doubao-seedream-5-0-260128",
        secret_ref=secret_ref,
        default_options={
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "project_name": "default",
            "size": "2K",
            "response_format": "url",
            "watermark": False,
            "n": 1,
        },
    )

    for index in range(2):
        invocation, result = gateway.invoke(
            ProviderCall(
                case_id="case_demo",
                provider_profile_id=profile.id,
                capability_id="image.generate",
                input={"prompt": "封面测试 cover prompt"},
                idempotency_key=f"cover-seedream-ak-{index}",
            )
        )
        assert invocation.status == ProviderStatus.succeeded
        assert result is not None
        _assert_cover_artifact_is_9_16(repository, result.output["cover_artifact_id"])

    assert len(openapi_bodies) == 1
    assert requests.count("POST ark.cn-beijing.volcengineapi.com/") == 1
    assert requests.count("POST ark.cn-beijing.volces.com/api/v3/images/generations") == 2


def test_openai_image_edits_from_reference_image(tmp_path):
    import base64

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/edits":
            seen["path"] = request.url.path
            seen["content_type"] = request.headers.get("content-type", "")
            # The reference image bytes ride in the multipart body, not a JSON prompt.
            seen["body"] = request.content
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(_PNG_1x1).decode("ascii")}]},
            )
        raise AssertionError(f"unexpected request to {request.url.path}")

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("image-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="openai.image",
        capability="image.generate",
        model_id="gpt-image-2-all",
        secret_ref=secret_ref,
        default_options={"base_url": "https://example.invalid/v1", "provider_kind": "neuromash"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="image.generate",
            input={
                "prompt": "封面测试 cover prompt",
                "reference_image_b64": base64.b64encode(_PNG_1x1).decode("ascii"),
                "reference_filename": "source-frame.png",
            },
            idempotency_key="cover-edit-1",
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    # Routed to the EDIT endpoint (template-conditioned), not plain generation.
    assert seen["path"] == "/v1/images/edits"
    assert "multipart/form-data" in seen["content_type"]
    assert b"source-frame.png" in seen["body"]
    assert result.output["reference_image_requested"] is True
    assert result.output["reference_image_used"] is True
    assert result.output["reference_transport"] == "images.edits"
    assert invocation.request_artifact_id in repository.artifacts
    request_artifact = repository.artifacts[invocation.request_artifact_id]
    assert request_artifact.kind.value == "provider.raw_request"
    request_path = gateway.object_store._path(parse_local_uri(request_artifact.uri))  # type: ignore[union-attr]
    assert request_path.read_bytes() == _PNG_1x1
    _assert_cover_artifact_is_9_16(repository, result.output["cover_artifact_id"])


def test_openai_image_edit_rejection_falls_back_to_generation(tmp_path):
    import base64

    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/images/edits":
            # Both image / image[] field attempts rejected with a request-shape error.
            return httpx.Response(400, text="edits unsupported")
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(_PNG_1x1).decode("ascii")}]}
            )
        raise AssertionError(f"unexpected {request.url.path}")

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("image-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="openai.image",
        capability="image.generate",
        model_id="gpt-image-2-all",
        secret_ref=secret_ref,
        default_options={"base_url": "https://example.invalid/v1", "provider_kind": "neuromash"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="image.generate",
            input={
                "prompt": "cover",
                "template_image_b64": base64.b64encode(_PNG_1x1).decode("ascii"),
                "template_filename": "ref.png",
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    # Tried edits (both fields) then degraded to generation; cover still produced.
    assert paths.count("/v1/images/edits") == 2
    assert paths[-1] == "/v1/images/generations"
    assert result.output["reference_image_requested"] is True
    assert result.output["reference_image_used"] is False
    assert result.output["reference_transport"] is None


def test_openai_image_falls_back_to_url_when_no_b64(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.invalid/cover.png"}]})
        assert str(request.url) == "https://cdn.invalid/cover.png"
        return httpx.Response(200, content=_PNG_1x1)

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("image-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="openai.image",
        capability="image.generate",
        model_id="gpt-image-2-all",
        secret_ref=secret_ref,
        default_options={"base_url": "https://example.invalid/v1", "provider_kind": "neuromash"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="image.generate",
            input={"prompt": "cover"},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    _assert_cover_artifact_is_9_16(repository, result.output["cover_artifact_id"])


def test_openai_image_http_errors_map_to_spec_codes(tmp_path):
    cases = [
        (httpx.Response(401, text="bad key"), ErrorCode.provider_auth_failed),
        (httpx.Response(429, text="quota"), ErrorCode.provider_quota_exceeded),
        (httpx.Response(500, text="boom"), ErrorCode.provider_remote_failed),
    ]
    for response, expected_code in cases:
        repository, gateway = _gateway(tmp_path, httpx.MockTransport(lambda request, response=response: response))
        secret_ref = gateway.secret_store.put("image-key")  # type: ignore[union-attr]
        profile = _profile(
            repository,
            provider_id="openai.image",
            capability="image.generate",
            model_id="gpt-image-2-all",
            secret_ref=secret_ref,
            default_options={"base_url": "https://example.invalid/v1"},
        )
        invocation, result = gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="image.generate",
                input={"prompt": "cover"},
            )
        )
        assert result is None
        assert invocation.error and invocation.error.code == expected_code


def test_openai_image_requires_active_secret(tmp_path):
    repository, gateway = _gateway(tmp_path, httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    profile = _profile(
        repository,
        provider_id="openai.image",
        capability="image.generate",
        model_id="gpt-image-2-all",
        secret_ref="missing.secret",
        default_options={"base_url": "https://example.invalid/v1"},
    )
    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="image.generate",
            input={"prompt": "cover"},
        )
    )
    # No active secret -> gateway rejects before any network call (no spend).
    assert result is None
    assert invocation.error and invocation.error.code == ErrorCode.provider_auth_failed


def test_openai_image_rejects_missing_prompt_bad_payloads_and_invalid_cover(tmp_path):
    def profile_for(handler):
        repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
        secret_ref = gateway.secret_store.put("image-key")  # type: ignore[union-attr]
        profile = _profile(
            repository,
            provider_id="openai.image",
            capability="image.generate",
            model_id="gpt-image-1",
            secret_ref=secret_ref,
            default_options={"base_url": "https://example.invalid/v1"},
        )
        return gateway, profile

    gateway, profile = profile_for(lambda request: httpx.Response(500, text="should not call"))
    invocation, result = gateway.invoke(
        ProviderCall(provider_profile_id=profile.id, capability_id="image.generate", input={})
    )
    assert result is None
    assert invocation.error and invocation.error.message == "Image prompt is required."

    cases = [
        ({"data": [{}]}, "Image provider returned no image data."),
        ({"data": [{"b64_json": "x"}]}, "Image provider returned invalid base64."),
        (
            {"data": [{"b64_json": base64.b64encode(b"not an image").decode("ascii")}]},
            "Image provider returned a cover that could not be normalized to 9:16.",
        ),
    ]
    for payload, expected_message in cases:
        gateway, profile = profile_for(lambda request, payload=payload: httpx.Response(200, json=payload))
        invocation, result = gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="image.generate",
                input={"prompt": "cover"},
            )
        )
        assert result is None
        assert invocation.error and invocation.error.message == expected_message


def test_openai_image_invalid_reference_falls_back_without_request_artifact(tmp_path):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(_PNG_1x1).decode("ascii")}],
                },
            )
        raise AssertionError(f"bad reference should not be sent to {request.url.path}")

    repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
    secret_ref = gateway.secret_store.put("image-key")  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="openai.image",
        capability="image.generate",
        model_id="gpt-image-1",
        secret_ref=secret_ref,
        default_options={"base_url": "https://example.invalid/v1"},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="image.generate",
            input={
                "prompt": "cover",
                "reference_image_b64": "data:image/png,not-base64",
                "reference_filename": "bad.png",
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert invocation.request_artifact_id is None
    assert result is not None
    assert result.output["reference_image_requested"] is True
    assert result.output["reference_image_used"] is False
    assert paths == ["/v1/images/generations"]


def test_openai_image_helper_edges():
    encoded = base64.b64encode(_PNG_1x1).decode("ascii")

    assert openai_image_mod.OpenAIImageProvider._b64_to_bytes(
        f"data:image/png;base64,{encoded}"
    ) == _PNG_1x1
    with pytest.raises(ProviderRuntimeError) as exc_info:
        openai_image_mod.OpenAIImageProvider._b64_to_bytes("x")
    assert exc_info.value.code == ErrorCode.provider_remote_failed

    provider = openai_image_mod.OpenAIImageProvider(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert provider._url_to_bytes(f"data:image/png;base64,{encoded}", "key", None) == _PNG_1x1  # type: ignore[arg-type]

    assert openai_image_mod._bool_option(" YES ") is True
    assert openai_image_mod._bool_option("off") is False
    assert openai_image_mod._bool_option(1) is True
    assert openai_image_mod._reference_image_bytes(f"  {encoded}\n") == _PNG_1x1
    assert openai_image_mod._reference_image_bytes(f"data:image/png;base64,{encoded}") == _PNG_1x1
    assert openai_image_mod._reference_image_bytes("") is None
    assert openai_image_mod._reference_image_bytes("data:image/png,not-base64") is None
    assert openai_image_mod._reference_image_bytes("bad-base64") is None
    assert openai_image_mod._image_data_uri("", "ref.png") is None
    assert openai_image_mod._image_data_uri("data:image/jpeg;base64,abc", "ref.jpg") == "data:image/jpeg;base64,abc"
    assert openai_image_mod._image_data_uri("bad-base64", "ref.png") is None
    assert openai_image_mod._image_data_uri(encoded, "ref.jpg") == f"data:image/jpeg;base64,{encoded}"
    assert openai_image_mod._clean_filename("https://cdn.example/path/ref.png?x=1", "fallback.png") == "ref.png"
    assert openai_image_mod._clean_filename("", "fallback.png") == "fallback.png"


def test_volcengine_seedream_access_key_validation_and_openapi_errors(tmp_path):
    def invoke_seedream(handler, *, model_id: str = "doubao-seedream"):
        repository, gateway = _gateway(tmp_path, httpx.MockTransport(handler))
        secret_ref = gateway.secret_store.put("AKLTxxx:sk-secret")  # type: ignore[union-attr]
        profile = _profile(
            repository,
            provider_id="volcengine.seedream",
            capability="image.generate",
            model_id=model_id,
            secret_ref=secret_ref,
            default_options={"base_url": "https://ark.cn-beijing.volces.com/api/v3"},
        )
        return gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="image.generate",
                input={"prompt": "cover"},
            )
        )

    invocation, result = invoke_seedream(
        lambda request: httpx.Response(500, text="should not call"),
        model_id="",
    )
    assert result is None
    assert invocation.error and invocation.error.code == ErrorCode.provider_unsupported_option

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

        invocation, result = invoke_seedream(handler)
        assert result is None
        assert invocation.error and invocation.error.code == expected

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    invocation, result = invoke_seedream(timeout_handler)
    assert result is None
    assert invocation.error and invocation.error.code == ErrorCode.provider_timeout
