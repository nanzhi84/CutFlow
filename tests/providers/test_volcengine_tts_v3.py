"""Tests for the VolcengineTTSProvider v3 unidirectional-streaming path.

v3 is opted into per-profile via ``default_options.api_version == "v3"`` and, unlike
v1, returns character-level timestamps for cloned (``S_``-prefixed) voices. HTTP is
mocked: the management plane (open.volcengineapi.com, issues the x-api-key) and the
v3 data plane (openspeech ``/api/v3/tts/unidirectional``, a chunked stream of JSON
lines) are dispatched by URL. Stored audio is a real tiny mp3 so probe_media works.
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
        [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", "0.05", "-b:a", "32k", "-f", "mp3", "-",
        ],
        capture_output=True,
    )
    assert proc.returncode == 0 and proc.stdout, proc.stderr
    return proc.stdout


def _context(tmp_path, *, options: dict | None = None) -> ProviderInvocationContext:
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    secret_ref = secret_store.put("ak-id:sk-secret", secret_ref="volc_tts_prod.secret")
    default_options = {"appid": "9635790622", "cluster": "volcano_icl"}
    if options:
        default_options.update(options)
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
        default_options=default_options,
    )
    repository.provider_profiles[profile.id] = profile
    return ProviderInvocationContext(
        repository=repository,
        profile=profile,
        invocation_id="pinv_volc_v3",
        secret_store=secret_store,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _call(ctx, **input_kwargs) -> ProviderCall:
    return ProviderCall(
        case_id="c",
        provider_profile_id=ctx.profile.id,
        capability_id="tts.speech",
        input=input_kwargs,
        idempotency_key="k",
    )


def _list_api_keys_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"Result": {"APIKeys": [{"Name": "cutagent-tts", "APIKey": "xk-123", "Disable": False}]}},
    )


def _v3_stream(audio_chunks: list[bytes], sentence: dict | None) -> bytes:
    """Render a v3 response body: newline-delimited JSON events (chunked framing)."""
    lines: list[str] = []
    for chunk in audio_chunks:
        lines.append(json.dumps({"code": 0, "message": "", "data": base64.b64encode(chunk).decode()}))
    if sentence is not None:
        lines.append(json.dumps({"code": 0, "message": "", "data": "", "sentence": sentence}))
    lines.append(json.dumps({"code": 20000000, "message": "OK"}))
    return ("\n".join(lines) + "\n").encode("utf-8")


_SENTENCE = {
    "phonemes": [],
    "text": "你好世界",
    "words": [
        {"word": "你", "startTime": 0.085, "endTime": 0.235, "confidence": 0.9},
        {"word": "好，", "startTime": 0.235, "endTime": 0.525, "confidence": 0.93},
        {"word": "世", "startTime": 0.805, "endTime": 0.975, "confidence": 0.95},
        {"word": "界", "startTime": 0.975, "endTime": 1.165, "confidence": 0.97},
    ],
}


def test_speech_v1_is_the_default_route(tmp_path, tiny_mp3) -> None:
    """No api_version option → v1 ``/api/v1/tts`` path, unchanged."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            return _list_api_keys_response()
        seen.append(url)
        assert url.endswith("/api/v1/tts")
        return httpx.Response(
            200,
            json={
                "code": 3000,
                "message": "Success",
                "data": base64.b64encode(tiny_mp3).decode(),
                "addition": {"duration": "500"},
            },
        )

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path)  # no api_version
    result = provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好世界", voice_id="S_UDXV2pG62"), ctx
    )
    assert result.output["audio_artifact_id"]
    assert seen and seen[0].endswith("/api/v1/tts")


def test_speech_v3_routes_and_builds_request(tmp_path, tiny_mp3) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            return _list_api_keys_response()
        assert url.endswith("/api/v3/tts/unidirectional")
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_v3_stream([tiny_mp3], _SENTENCE))

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    result = provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好世界", voice_id="S_UDXV2pG62"), ctx
    )
    headers = captured["headers"]
    assert headers["x-api-key"] == "xk-123"  # new-console single-key auth
    assert headers["x-api-resource-id"] == "volc.megatts.default"  # cloned voice → timestamps
    assert headers["x-api-request-id"]
    body = captured["body"]
    assert body["req_params"]["speaker"] == "S_UDXV2pG62"
    assert body["req_params"]["audio_params"]["enable_timestamp"] is True
    assert body["req_params"]["audio_params"]["format"] == "mp3"
    additions = json.loads(body["req_params"]["additions"])
    assert additions["enable_timestamp"] is True
    assert "model" not in body["req_params"]  # no model unless v3_model option set
    assert result.output["voice_id"] == "S_UDXV2pG62"
    assert result.output["audio_artifact_id"]
    assert result.input_tokens == 4
    assert result.estimated_cost is not None


def test_speech_v3_concatenates_audio_and_extracts_timestamps(tmp_path, tiny_mp3) -> None:
    half = len(tiny_mp3) // 2
    chunks = [tiny_mp3[:half], tiny_mp3[half:]]

    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        return httpx.Response(200, content=_v3_stream(chunks, _SENTENCE))

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    result = provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好世界", voice_id="S_UDXV2pG62"), ctx
    )
    timing = result.output["timing"]
    assert timing is not None
    # camelCase startTime/endTime are already seconds — no /1000 conversion
    assert timing["tokens"] == [
        {"text": "你", "start": 0.085, "end": 0.235},
        {"text": "好，", "start": 0.235, "end": 0.525},
        {"text": "世", "start": 0.805, "end": 0.975},
        {"text": "界", "start": 0.975, "end": 1.165},
    ]
    # punctuation-merged token ("好，", len 2) → token granularity, not character
    assert timing["granularity"] == "token"
    assert timing["text_basis"] == "original"
    assert timing["segments"] == [{"text": "你好世界", "start": 0.085, "end": 1.165}]


def test_speech_v3_character_granularity_when_all_single_char(tmp_path, tiny_mp3) -> None:
    sentence = {
        "text": "你好",
        "words": [
            {"word": "你", "startTime": 0.1, "endTime": 0.3},
            {"word": "好", "startTime": 0.3, "endTime": 0.5},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        return httpx.Response(200, content=_v3_stream([tiny_mp3], sentence))

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    result = provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好", voice_id="S_X"), ctx
    )
    assert result.output["timing"]["granularity"] == "character"


def test_speech_v3_snake_case_milliseconds_fallback(tmp_path, tiny_mp3) -> None:
    """Legacy snake_case start_time/end_time is tolerated as milliseconds."""
    sentence = {
        "text": "你好",
        "words": [{"word": "你好", "start_time": 100, "end_time": 800}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        return httpx.Response(200, content=_v3_stream([tiny_mp3], sentence))

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    result = provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好", voice_id="S_X"), ctx
    )
    assert result.output["timing"]["tokens"] == [{"text": "你好", "start": 0.1, "end": 0.8}]


def test_speech_v3_missing_timestamps_yields_none(tmp_path, tiny_mp3) -> None:
    empty_sentence = {"phonemes": [], "text": "你好世界", "words": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        return httpx.Response(200, content=_v3_stream([tiny_mp3], empty_sentence))

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    result = provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好世界", voice_id="S_X"), ctx
    )
    assert result.output["timing"] is None
    assert result.output["audio_artifact_id"]  # audio still stored


def test_speech_v3_preset_voice_resource_and_speed(tmp_path, tiny_mp3) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_v3_stream([tiny_mp3], _SENTENCE))

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好世界", voice_id="zh_female_cancan", speed=1.5), ctx
    )
    # non-S_ voice defaults to the preset resource id
    assert captured["headers"]["x-api-resource-id"] == "seed-tts-2.0"
    # speed 1.5 → speech_rate +50 (v3 integer scale, not v1 ratio)
    assert captured["body"]["req_params"]["audio_params"]["speech_rate"] == 50


def test_speech_v3_resource_id_option_override(tmp_path, tiny_mp3) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_v3_stream([tiny_mp3], _SENTENCE))

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(
        tmp_path,
        options={"api_version": "v3", "resource_id": "seed-icl-2.0", "v3_model": "seed-tts-2.0-standard"},
    )
    provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好世界", voice_id="S_UDXV2pG62"), ctx
    )
    assert captured["headers"]["x-api-resource-id"] == "seed-icl-2.0"
    assert captured["body"]["req_params"]["model"] == "seed-tts-2.0-standard"


def test_speech_v3_stream_error_code_raises(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        body = json.dumps({"code": 45000001, "message": "[Invalid argument] InvalidModel"}) + "\n"
        return httpx.Response(200, content=body.encode())

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(ctx, operation="speech", text="你好", voice_id="S_X"), ctx
        )
    assert excinfo.value.code == ErrorCode.provider_remote_failed
    assert "45000001" in str(excinfo.value)


def test_speech_v3_http_error_maps_to_auth_failed(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        return httpx.Response(401, json={"header": {"code": 45000010, "message": "grant not found"}})

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(ctx, operation="speech", text="你好", voice_id="S_X"), ctx
        )
    assert excinfo.value.code == ErrorCode.provider_auth_failed


def test_speech_v3_no_audio_raises(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        body = json.dumps({"code": 20000000, "message": "OK"}) + "\n"
        return httpx.Response(200, content=body.encode())

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(ctx, operation="speech", text="你好", voice_id="S_X"), ctx
        )
    assert excinfo.value.code == ErrorCode.provider_remote_failed


def test_speech_v3_tolerates_sse_data_prefix(tmp_path, tiny_mp3) -> None:
    """The ``/unidirectional/sse`` framing prefixes each line with ``data: ``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        raw = _v3_stream([tiny_mp3], _SENTENCE).decode().splitlines()
        sse = "".join(f"data: {line}\n\n" for line in raw)
        return httpx.Response(200, content=sse.encode())

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(tmp_path, options={"api_version": "v3"})
    result = provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好世界", voice_id="S_UDXV2pG62"), ctx
    )
    assert result.output["timing"] is not None
    assert len(result.output["timing"]["tokens"]) == 4
