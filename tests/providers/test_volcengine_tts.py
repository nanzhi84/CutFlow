"""Tests for VolcengineTTSProvider (data plane synth/clone + management sync).

Synthesis/clone store real audio, so probe_media needs a real (tiny) mp3 — built
via ffmpeg and skipped if ffmpeg is unavailable. HTTP is mocked: the management
plane (open.volcengineapi.com, AK/SK) and the data plane (openspeech, x-api-key /
Bearer) are dispatched by URL.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess

import httpx
import pytest

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderRuntimeError
from packages.ai.providers.volcengine_tts import VolcengineTTSProvider
from packages.core.contracts import ErrorCode, ProviderOptionsSchemaRef, ProviderProfile
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore


def _ffmpeg_bin() -> str | None:
    return os.environ.get("CUTAGENT_FFMPEG_BIN") or shutil.which("ffmpeg")


@pytest.fixture
def tiny_mp3() -> bytes:
    ffmpeg = _ffmpeg_bin()
    if ffmpeg is None:
        pytest.skip("ffmpeg required to build the audio fixture")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=24000:cl=mono", "-t", "0.05", "-b:a", "32k", "-f", "mp3", "-"],
        capture_output=True,
    )
    assert proc.returncode == 0 and proc.stdout, proc.stderr
    return proc.stdout


def _context(tmp_path) -> ProviderInvocationContext:
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    secret_ref = secret_store.put("ak-id:sk-secret", secret_ref="volc_tts_prod.secret")
    profile = ProviderProfile(
        id="volcengine.tts.test",
        provider_id="volcengine.tts",
        model_id="seed-icl-2.0",
        capability="tts.speech",
        display_name="Volcengine TTS Test",
        environment="prod",
        secret_ref=secret_ref,
        timeout_sec=60,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
        default_options={"appid": "9635790622", "cluster": "volcano_icl"},
    )
    repository.provider_profiles[profile.id] = profile
    return ProviderInvocationContext(
        repository=repository,
        profile=profile,
        invocation_id="pinv_volc_test",
        secret_store=secret_store,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _call(tmp_path, ctx, **input_kwargs) -> ProviderCall:
    return ProviderCall(
        case_id="c",
        provider_profile_id=ctx.profile.id,
        capability_id="tts.speech",
        input=input_kwargs,
        idempotency_key="k",
    )


def _list_api_keys_response() -> httpx.Response:
    return httpx.Response(
        200, json={"Result": {"APIKeys": [{"Name": "cutagent-tts", "APIKey": "xk-123", "Disable": False}]}}
    )


def test_speech_synthesizes_and_stores(tmp_path, tiny_mp3) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            assert request.url.params["Action"] == "ListAPIKeys"
            return _list_api_keys_response()
        if url.endswith("/api/v1/tts"):
            assert request.headers["x-api-key"] == "xk-123"
            payload = json.loads(request.content)
            assert payload["app"]["cluster"] == "volcano_icl"
            assert payload["audio"]["voice_type"] == "S_UDXV2pG62"
            assert payload["request"]["operation"] == "query"
            return httpx.Response(
                200,
                json={
                    "code": 3000,
                    "message": "Success",
                    "data": base64.b64encode(tiny_mp3).decode("ascii"),
                    "addition": {"duration": "1966"},
                },
            )
        raise AssertionError(f"unexpected url {url}")

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path)
    result = provider.invoke_with_context(
        _call(tmp_path, ctx, operation="speech", text="你好世界", voice_id="S_UDXV2pG62"), ctx
    )
    assert result.output["voice_id"] == "S_UDXV2pG62"
    assert result.output["audio_artifact_id"]
    assert result.input_tokens == 4  # len("你好世界")
    assert result.estimated_cost is not None


def test_speech_non_success_code_raises(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        return httpx.Response(200, json={"code": 3001, "message": "quota exhausted"})

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path)
    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(tmp_path, ctx, operation="speech", text="hi", voice_id="S_X"), ctx
        )
    assert excinfo.value.code == ErrorCode.provider_remote_failed


def test_voice_list_syncs_cloned_voices(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["Action"] == "ListMegaTTSTrainStatus"
        return httpx.Response(
            200,
            json={"Result": {"Statuses": [
                {"SpeakerID": "S_UDXV2pG62", "Alias": "无忧快喷", "State": "Success", "DemoAudio": "https://x/d.wav"},
                {"SpeakerID": "S_SLOT", "Alias": "", "State": "Unknown"},
            ]}},
        )

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path)
    result = provider.invoke_with_context(_call(tmp_path, ctx, operation="voice_list"), ctx)
    voices = result.output["voices"]
    assert len(voices) == 1  # empty slot filtered
    assert voices[0]["voice_id"] == "S_UDXV2pG62"
    assert voices[0]["status"] == "ready"
    assert voices[0]["preview_url"] == "https://x/d.wav"


def test_clone_claims_free_slot_and_returns_training(tmp_path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            action = request.url.params["Action"]
            seen.append(action)
            if action == "ListMegaTTSTrainStatus":
                return httpx.Response(
                    200,
                    json={"Result": {"Statuses": [
                        {"SpeakerID": "S_DONE", "Alias": "已用", "State": "Success"},
                        {"SpeakerID": "S_FREE", "Alias": "", "State": "Unknown"},
                    ]}},
                )
            if action == "ListAPIKeys":
                return _list_api_keys_response()
            raise AssertionError(action)
        if url.endswith("/api/v1/mega_tts/audio/upload"):
            assert request.headers["Authorization"] == "Bearer;xk-123"
            assert request.headers["Resource-Id"] == "volc.megatts.voiceclone"
            payload = json.loads(request.content)
            assert payload["appid"] == "9635790622"
            assert payload["speaker_id"] == "S_FREE"  # claimed the empty slot
            assert payload["audios"][0]["audio_bytes"]
            return httpx.Response(200, json={"code": 0, "message": "success"})
        raise AssertionError(f"unexpected url {url}")

    # reference audio on disk
    audio = tmp_path / "ref.mp3"
    audio.write_bytes(b"\xff\xfb\x90\x00reference-audio-bytes")
    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path)
    result = provider.invoke_with_context(
        _call(tmp_path, ctx, operation="clone", display_name="我的声音",
              reference_audio_uri=str(audio)), ctx
    )
    assert result.output["voice_id"] == "S_FREE"
    assert result.output["status"] == "training"
    assert result.output["display_name"] == "我的声音"
    assert "ListMegaTTSTrainStatus" in seen


def test_design_is_unsupported(tmp_path) -> None:
    provider = VolcengineTTSProvider(_client(lambda r: httpx.Response(200, json={})))
    ctx = _context(tmp_path)
    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(_call(tmp_path, ctx, operation="design", prompt="x"), ctx)
    assert excinfo.value.code == ErrorCode.provider_unsupported_option


def test_wrong_capability_rejected(tmp_path) -> None:
    provider = VolcengineTTSProvider(_client(lambda r: httpx.Response(200, json={})))
    ctx = _context(tmp_path)
    call = ProviderCall(
        case_id="c", provider_profile_id=ctx.profile.id, capability_id="llm.chat",
        input={"operation": "speech"}, idempotency_key="k",
    )
    with pytest.raises(ProviderRuntimeError):
        provider.invoke_with_context(call, ctx)
