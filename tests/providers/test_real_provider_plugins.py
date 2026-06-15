from __future__ import annotations

import httpx

from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway
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
        "openai.image",
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
    assert result.output["subtitle_segments"] == [
        {"start": 0.0, "end": 0.5, "text": "第一句。"},
        {"start": 0.5, "end": 1.0, "text": "第二句。"},
    ]
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
    assert artifact.media_info
    assert artifact.media_info.media_type == "video"
    assert "POST /api/v1/services/aigc/image2video/video-synthesis/" in requests


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


def test_minimax_voice_design_stores_inline_preview_audio(tmp_path, media_fixture_factory):
    preview_audio = media_fixture_factory.audio(duration_sec=1.0, filename="design-preview.wav").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/voice_design"
        assert request.url.params["GroupId"] == "group-1"
        body = __import__("json").loads(request.content)
        assert body["voice_prompt"] == "calm product narrator"
        assert body["preview_text"] == "试听文本"
        return httpx.Response(
            200,
            json={
                "base_resp": {"status_code": 0},
                "data": {
                    "voice_id": "voice_design_1",
                    "preview_audio": preview_audio.hex(),
                },
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
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={
                "operation": "design",
                "display_name": "Provider Design",
                "prompt": "calm product narrator",
                "preview_text": "试听文本",
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["voice_id"] == "voice_design_1"
    artifact = repository.artifacts[result.output["preview_audio_artifact_id"]]
    assert artifact.sha256
    assert artifact.media_info
    assert artifact.media_info.media_type == "audio"


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
        "source": "asr",
    }
    assert result.audio_seconds == 2.1
    assert requests == [
        "POST /api/v1/services/audio/asr/transcription",
        "GET /api/v1/tasks/asr-task-1",
        "GET /api/v1/tasks/asr-task-1",
        "GET /asr-result.json",
    ]


def test_dashscope_llm_uses_compatible_chat_base_url_and_options(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer dashscope-key"
        body = __import__("json").loads(request.content)
        assert body == {
            "model": "qwen-plus",
            "messages": [{"role": "user", "content": "Return JSON."}],
            "temperature": 0.7,
            "max_tokens": 2000,
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
        model_id="qwen-plus",
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


def test_dashscope_vlm_uses_openai_compatible_multimodal_payload(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer dashscope-key"
        body = __import__("json").loads(request.content)
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
            "max_tokens": 1200,
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
    assert result.input_tokens == 21
    assert result.output_tokens == 13


def test_runninghub_heygem_records_external_job_and_stores_polled_video(
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
        if request.url.path == "/task/openapi/ai-app/run":
            return httpx.Response(200, json={"data": {"taskId": "rh-job-1"}})
        if request.url.path == "/task/openapi/status":
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.url.path == "/task/openapi/outputs":
            return httpx.Response(
                200,
                json={"data": {"fileUrl": "https://files.example/heygem-result.mp4", "consumeCoins": 3}},
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
    assert result is not None
    assert result.provider_credits == 3
    assert result.output["video_uri"].startswith("local://")
    artifact = repository.artifacts[result.output["video_artifact_id"]]
    assert artifact.media_info
    assert artifact.media_info.media_type == "video"
    assert "POST /task/openapi/status" in requests
    assert "POST /task/openapi/outputs" in requests


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


_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f"
    "15c4890000000b49444154789c6360000200000500017a5eab3f00000000"
    "49454e44ae426082"
)


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
    artifact = repository.artifacts[result.output["cover_artifact_id"]]
    assert artifact.kind.value == "cover.image"
    assert artifact.media_info and artifact.media_info.media_type == "image"
    object_path = gateway.object_store._path(parse_local_uri(result.output["cover_uri"]))  # type: ignore[union-attr]
    assert object_path.read_bytes() == _PNG_1x1


def test_openai_image_edits_from_template_reference(tmp_path):
    import base64
    import json

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
                "template_image_b64": base64.b64encode(_PNG_1x1).decode("ascii"),
                "template_filename": "ref.png",
            },
            idempotency_key="cover-edit-1",
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    # Routed to the EDIT endpoint (template-conditioned), not plain generation.
    assert seen["path"] == "/v1/images/edits"
    assert "multipart/form-data" in seen["content_type"]
    assert b"ref.png" in seen["body"]
    artifact = repository.artifacts[result.output["cover_artifact_id"]]
    assert artifact.media_info and artifact.media_info.media_type == "image"


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
    object_path = gateway.object_store._path(parse_local_uri(result.output["cover_uri"]))  # type: ignore[union-attr]
    assert object_path.read_bytes() == _PNG_1x1


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
