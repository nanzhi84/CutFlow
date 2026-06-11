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

    assert {"sandbox", "minimax.tts", "dashscope.asr", "dashscope.vlm", "runninghub.heygem", "dashscope.llm"} <= set(
        gateway.plugins
    )

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
