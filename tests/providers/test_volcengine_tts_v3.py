"""Tests for the full-file Volcengine ICL 2.0 ``submit → query`` path."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess

import httpx
import pytest

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderGateway,
    ProviderRuntimeError,
)
from packages.ai.gateway.sqlalchemy_repository import (
    SqlAlchemyProviderInvocationStore,
    SqlAlchemyProviderRuntimeRepository,
)
from packages.ai.providers.volcengine_tts import VolcengineTTSProvider
from packages.core.contracts import (
    ErrorCode,
    ProviderInvocation,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
)
from packages.core.provider_idempotency import build_provider_call_idempotency_key
from packages.core.storage.database import SecretRow
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.provider_seed import seed_real_provider_configuration
from packages.core.storage.repository import Repository, new_id
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
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            "0.05",
            "-b:a",
            "32k",
            "-f",
            "mp3",
            "-",
        ],
        capture_output=True,
    )
    assert proc.returncode == 0 and proc.stdout, proc.stderr
    return proc.stdout


def _context(
    tmp_path,
    *,
    options: dict | None = None,
    invocation_status: ProviderStatus = ProviderStatus.submitted,
) -> ProviderInvocationContext:
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    secret_ref = secret_store.put(
        json.dumps(
            {
                "access_key_id": "ak-id",
                "secret_access_key": "sk-secret",
                "access_token": "app-token-123",
            }
        ),
        secret_ref="volc_tts_prod.secret",
    )
    default_options = {
        "appid": "9635790622",
        "cluster": "volcano_icl",
        "async_icl2_ready": True,
        "poll_interval": 0,
        "poll_max_attempts": 3,
    }
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
    invocation = ProviderInvocation(
        id="pinv_volc_v3",
        idempotency_key="k",
        provider_id=profile.provider_id,
        model_id=profile.model_id,
        provider_profile_id=profile.id,
        capability_id=profile.capability,
        status=invocation_status,
        external_job_id=("task-resume" if invocation_status is ProviderStatus.polling else None),
    )
    repository.provider_invocations[invocation.id] = invocation
    return ProviderInvocationContext(
        repository=repository,
        profile=profile,
        invocation_id=invocation.id,
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


def _gateway_credentials(*, include_token: bool = True) -> str:
    payload = {
        "access_key_id": "ak-id",
        "secret_access_key": "sk-secret",
    }
    if include_token:
        payload["access_token"] = "app-token-123"
    return json.dumps(payload)


def _durable_gateway_case(
    db_session_factory,
    tmp_path,
    *,
    handler,
    profile_id: str,
    object_store=None,
):
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / f"{profile_id}-secrets")
    secret_ref = secret_store.put(
        _gateway_credentials(),
        secret_ref=f"{profile_id}.secret",
    )
    with db_session_factory() as session:
        session.add(
            SecretRow(
                id=new_id("sec"),
                provider_id="volcengine.tts",
                environment="prod",
                name=f"{profile_id} test secret",
                secret_ref=secret_ref,
                status="active",
            )
        )
        session.commit()
    profile = ProviderProfile(
        id=profile_id,
        provider_id="volcengine.tts",
        model_id="seed-icl-2.0",
        capability="tts.speech",
        display_name="Volcengine TTS Gateway Test",
        environment="prod",
        secret_ref=secret_ref,
        timeout_sec=60,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
        default_options={
            "appid": "9635790622",
            "api_version": "v3",
            "resource_id": "seed-icl-2.0",
            "format": "mp3",
            "sample_rate": 24000,
            "async_icl2_ready": True,
            "poll_interval": 0,
            "poll_max_attempts": 1,
        },
    )
    repository.provider_profiles[profile.id] = profile
    gateway = ProviderGateway(
        repository,
        provider_reader=SqlAlchemyProviderRuntimeRepository(db_session_factory),
        secret_store=secret_store,
        object_store=object_store or LocalObjectStore(tmp_path / f"{profile_id}-objects"),
        auto_register_real_plugins=False,
    )
    gateway.register(VolcengineTTSProvider(_client(handler)))
    key = build_provider_call_idempotency_key(
        job_id=new_id("job"),
        canonical_node_id="TTS",
        logical_call_slot="tts:full-script-single-file:v2",
        provider_profile_id=profile.id,
        input_manifest_hash="manifest-icl2",
    )
    call = ProviderCall(
        provider_profile_id=profile.id,
        capability_id="tts.speech",
        idempotency_key=key,
        input={"text": "你好世界", "voice_id": "S_UDXV2pG62"},
    )
    return (
        gateway,
        SqlAlchemyProviderInvocationStore(db_session_factory),
        call,
        secret_store,
        secret_ref,
    )


class _MissingOnceObjectStore(LocalObjectStore):
    def __init__(self, root) -> None:
        super().__init__(root)
        self._missing_once = True

    def exists(self, ref) -> bool:
        if self._missing_once:
            self._missing_once = False
            return False
        return super().exists(ref)


def _list_api_keys_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "Result": {
                "APIKeys": [
                    {"Name": "cutagent-tts", "APIKey": "xk-123", "Disable": False}
                ]
            }
        },
    )


_SENTENCES = [
    {
        "text": "你好，",
        "words": [
            {"word": "你", "startTime": 0.085, "endTime": 0.235},
            {"word": "好，", "startTime": 0.235, "endTime": 0.525},
        ],
    },
    {
        "text": "世界",
        "words": [
            {"word": "世", "startTime": 0.805, "endTime": 0.975},
            {"word": "界", "startTime": 0.975, "endTime": 1.165},
        ],
    },
]


def _submit_response(task_id: str = "task-123") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 20000000,
            "message": "ok",
            "data": {"req_text_length": 4, "task_id": task_id, "task_status": 1},
        },
    )


def _query_response(
    status: int,
    *,
    audio_url: str | None = "https://audio.example/complete.mp3",
    sentences: object | None = _SENTENCES,
    message: str | None = None,
) -> httpx.Response:
    data: dict[str, object] = {"task_id": "task-123", "task_status": status}
    if audio_url is not None:
        data["audio_url"] = audio_url
    if sentences is not None:
        data["sentences"] = sentences
    if message is not None:
        data["message"] = message
    return httpx.Response(200, json={"code": 20000000, "message": "ok", "data": data})


def _assert_strict_decode(ctx: ProviderInvocationContext, audio_uri: str) -> None:
    ffmpeg = _ffmpeg_bin()
    if ffmpeg is None:
        pytest.skip("ffmpeg required to validate the audio fixture")
    path = ctx.local_path_for_uri(audio_uri)
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def test_speech_v1_is_the_default_route(tmp_path, tiny_mp3) -> None:
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
    ctx = _context(tmp_path)
    result = provider.invoke_with_context(
        _call(ctx, operation="speech", text="你好世界", voice_id="S_UDXV2pG62"), ctx
    )

    assert result.output["audio_artifact_id"]
    assert seen == ["https://openspeech.bytedance.com/api/v1/tts"]


def test_seeded_production_profile_uses_async_icl2_full_mp3() -> None:
    repository = Repository()
    seed_real_provider_configuration(repository)

    profile = repository.provider_profiles["volcengine.tts.prod"]
    assert profile.model_id == "seed-icl-2.0"
    assert profile.default_options["api_version"] == "v3"
    assert profile.default_options["resource_id"] == "seed-icl-2.0"
    assert profile.default_options["format"] == "mp3"
    assert profile.default_options["sample_rate"] == 24000
    assert profile.default_options["async_icl2_ready"] is False
    capability = repository.provider_capabilities["cap_volcengine_tts_prod"]
    assert capability.supports_async_job is True


def test_async_icl2_submits_polls_downloads_and_stores_one_original_mp3(
    tmp_path, tiny_mp3
) -> None:
    captured: dict[str, object] = {"queries": []}
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            return _list_api_keys_response()
        if url.endswith("/api/v3/tts/submit"):
            captured["submit_headers"] = dict(request.headers)
            captured["submit_body"] = json.loads(request.content)
            return _submit_response()
        if url.endswith("/api/v3/tts/query"):
            query_count += 1
            captured["queries"].append(json.loads(request.content))
            return _query_response(1 if query_count == 1 else 2)
        if url == "https://audio.example/complete.mp3":
            return httpx.Response(200, content=tiny_mp3)
        raise AssertionError(f"unexpected request: {url}")

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(
        tmp_path,
        options={
            "api_version": "v3",
            "resource_id": "seed-icl-2.0",
            "format": "mp3",
            "sample_rate": 24000,
        },
    )
    result = provider.invoke_with_context(
        _call(
            ctx,
            operation="speech",
            text="你好世界",
            voice_id="S_UDXV2pG62",
        ),
        ctx,
    )

    headers = captured["submit_headers"]
    assert headers["x-api-app-id"] == "9635790622"
    assert headers["x-api-access-key"] == "app-token-123"
    assert "x-api-key" not in headers
    assert headers["x-api-resource-id"] == "seed-icl-2.0"
    body = captured["submit_body"]
    assert body["unique_id"] == headers["x-api-request-id"]
    assert body["req_params"] == {
        "text": "你好世界",
        "speaker": "S_UDXV2pG62",
        "audio_params": {
            "format": "mp3",
            "sample_rate": 24000,
            "enable_timestamp": True,
        },
    }
    assert captured["queries"] == [{"task_id": "task-123"}, {"task_id": "task-123"}]
    invocation = ctx.repository.provider_invocations[ctx.invocation_id]
    assert invocation.status is ProviderStatus.polling
    assert invocation.external_job_id == "task-123"
    assert result.output["audio_uri"].endswith(".mp3")
    assert ctx.local_path_for_uri(result.output["audio_uri"]).read_bytes() == tiny_mp3
    assert result.raw_usage == {
        "characters": 4,
        "model": "seed-icl-2.0",
        "task_id": "task-123",
        "poll_attempts": 2,
        "source_format": "mp3",
        "stored_format": "mp3",
    }
    _assert_strict_decode(ctx, result.output["audio_uri"])


def test_async_icl2_rejects_legacy_ak_sk_without_application_token(tmp_path) -> None:
    ctx = _context(
        tmp_path,
        options={
            "api_version": "v3",
            "resource_id": "seed-icl-2.0",
            "format": "mp3",
            "sample_rate": 24000,
        },
    )
    assert ctx.secret_store is not None
    ctx.secret_store.put("ak-id:sk-secret", secret_ref="volc_tts_prod.secret")
    provider = VolcengineTTSProvider(
        _client(lambda request: pytest.fail(f"unexpected request: {request.url}"))
    )

    with pytest.raises(ProviderRuntimeError) as exc_info:
        provider.invoke_with_context(
            _call(ctx, operation="speech", text="你好", voice_id="S_TEST"), ctx
        )

    assert exc_info.value.code == ErrorCode.provider_auth_failed
    assert "Access Token" in str(exc_info.value)


def test_async_icl2_stays_disabled_until_access_token_is_explicitly_armed(tmp_path) -> None:
    ctx = _context(
        tmp_path,
        options={
            "api_version": "v3",
            "resource_id": "seed-icl-2.0",
            "async_icl2_ready": False,
        },
    )
    provider = VolcengineTTSProvider(
        _client(lambda request: pytest.fail(f"unexpected request: {request.url}"))
    )

    with pytest.raises(ProviderRuntimeError) as exc_info:
        provider.invoke_with_context(
            _call(ctx, operation="speech", text="你好", voice_id="S_TEST"), ctx
        )

    assert exc_info.value.code == ErrorCode.provider_auth_failed
    assert "not armed" in str(exc_info.value)


def test_async_icl2_rejects_internal_model_voice_id(tmp_path) -> None:
    provider = VolcengineTTSProvider(
        _client(lambda request: pytest.fail(f"unexpected request: {request.url}"))
    )
    ctx = _context(
        tmp_path,
        options={"api_version": "v3", "resource_id": "seed-icl-2.0"},
    )

    with pytest.raises(ProviderRuntimeError) as exc_info:
        provider.invoke_with_context(
            _call(ctx, text="你好", voice_id="ICL_uranus_internal"), ctx
        )

    assert exc_info.value.code == ErrorCode.provider_unsupported_option
    assert "external S_ SpeakerID" in str(exc_info.value)


def test_async_icl2_extracts_sentence_and_word_timestamps(tmp_path, tiny_mp3) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            return _list_api_keys_response()
        if url.endswith("/submit"):
            return _submit_response()
        if url.endswith("/query"):
            return _query_response(2)
        return httpx.Response(200, content=tiny_mp3)

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(
        tmp_path, options={"api_version": "v3", "resource_id": "seed-icl-2.0"}
    )
    result = provider.invoke_with_context(
        _call(ctx, text="你好世界", voice_id="S_UDXV2pG62"), ctx
    )

    timing = result.output["timing"]
    assert timing["segments"] == [
        {"text": "你好，", "start": 0.085, "end": 0.525},
        {"text": "世界", "start": 0.805, "end": 1.165},
    ]
    assert timing["tokens"] == [
        {"text": "你", "start": 0.085, "end": 0.235},
        {"text": "好，", "start": 0.235, "end": 0.525},
        {"text": "世", "start": 0.805, "end": 0.975},
        {"text": "界", "start": 0.975, "end": 1.165},
    ]
    assert timing["granularity"] == "token"
    assert timing["text_basis"] == "original"


def test_async_icl2_resume_queries_existing_task_without_resubmitting(
    tmp_path, tiny_mp3
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            return _list_api_keys_response()
        seen.append(url)
        if url.endswith("/query"):
            assert json.loads(request.content) == {"task_id": "task-resume"}
            return _query_response(2)
        if url == "https://audio.example/complete.mp3":
            return httpx.Response(200, content=tiny_mp3)
        raise AssertionError(f"unexpected request: {url}")

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(
        tmp_path,
        options={"api_version": "v3", "resource_id": "seed-icl-2.0"},
        invocation_status=ProviderStatus.polling,
    )
    result = provider.resume_with_context(
        _call(ctx, text="你好世界", voice_id="S_UDXV2pG62"),
        ctx,
        "task-resume",
    )

    assert all(not url.endswith("/submit") for url in seen)
    assert result.raw_usage["task_id"] == "task-resume"


def test_gateway_retry_keeps_accepted_icl2_task_and_never_resubmits(
    db_session_factory,
    tmp_path,
    tiny_mp3,
) -> None:
    submit_count = 0
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_count, query_count
        url = str(request.url)
        if url.endswith("/api/v3/tts/submit"):
            submit_count += 1
            return _submit_response("task-durable")
        if url.endswith("/api/v3/tts/query"):
            query_count += 1
            if query_count == 1:
                return httpx.Response(503, json={"message": "temporary gateway failure"})
            return _query_response(2)
        if url == "https://audio.example/complete.mp3":
            return httpx.Response(200, content=tiny_mp3)
        raise AssertionError(f"unexpected request: {url}")

    gateway, store, call, _, _ = _durable_gateway_case(
        db_session_factory,
        tmp_path,
        handler=handler,
        profile_id="volcengine.tts.gateway-test",
    )
    key = call.idempotency_key
    assert key is not None

    first, first_result = gateway.invoke(call)

    assert first_result is None
    assert first.status is ProviderStatus.polling
    assert first.external_job_id == "task-durable"
    assert first.error is not None
    assert first.error.code is ErrorCode.provider_remote_failed
    durable = store.load_by_key(key)
    assert durable is not None
    assert durable.status is ProviderStatus.polling
    assert durable.external_job_id == "task-durable"

    recovered, result = gateway.invoke(call)

    assert result is not None
    assert recovered.status is ProviderStatus.succeeded
    assert result.raw_usage["task_id"] == "task-durable"
    assert submit_count == 1
    assert query_count == 2
    assert store.load_by_key(key).status is ProviderStatus.succeeded


def test_gateway_credential_failure_after_acceptance_keeps_task_and_never_resubmits(
    db_session_factory,
    tmp_path,
    tiny_mp3,
) -> None:
    submit_count = 0
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_count, query_count
        url = str(request.url)
        if url.endswith("/api/v3/tts/submit"):
            submit_count += 1
            return _submit_response("task-credential-recovery")
        if url.endswith("/api/v3/tts/query"):
            query_count += 1
            if query_count == 1:
                return _query_response(1)
            return _query_response(2)
        if url == "https://audio.example/complete.mp3":
            return httpx.Response(200, content=tiny_mp3)
        raise AssertionError(f"unexpected request: {url}")

    gateway, store, call, secret_store, secret_ref = _durable_gateway_case(
        db_session_factory,
        tmp_path,
        handler=handler,
        profile_id="volcengine.tts.gateway-credential-recovery",
    )
    key = call.idempotency_key
    assert key is not None

    first, first_result = gateway.invoke(call)

    assert first_result is None
    assert first.status is ProviderStatus.polling
    assert first.external_job_id == "task-credential-recovery"
    assert first.error is not None
    assert first.error.code is ErrorCode.provider_timeout

    secret_store.put(
        _gateway_credentials(include_token=False),
        secret_ref=secret_ref,
    )
    missing_credential, missing_credential_result = gateway.invoke(call)

    assert missing_credential_result is None
    assert missing_credential.status is ProviderStatus.polling
    assert missing_credential.external_job_id == "task-credential-recovery"
    assert missing_credential.error is not None
    assert missing_credential.error.code is ErrorCode.provider_auth_failed
    durable = store.load_by_key(key)
    assert durable is not None
    assert durable.status is ProviderStatus.polling
    assert durable.external_job_id == "task-credential-recovery"
    assert submit_count == 1
    assert query_count == 1

    secret_store.put(_gateway_credentials(), secret_ref=secret_ref)
    recovered, result = gateway.invoke(call)

    assert result is not None
    assert recovered.status is ProviderStatus.succeeded
    assert result.raw_usage["task_id"] == "task-credential-recovery"
    assert submit_count == 1
    assert query_count == 2
    assert store.load_by_key(key).status is ProviderStatus.succeeded


def test_gateway_object_store_failure_after_acceptance_keeps_task_and_never_resubmits(
    db_session_factory,
    tmp_path,
    tiny_mp3,
) -> None:
    submit_count = 0
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_count, query_count
        url = str(request.url)
        if url.endswith("/api/v3/tts/submit"):
            submit_count += 1
            return _submit_response("task-object-recovery")
        if url.endswith("/api/v3/tts/query"):
            query_count += 1
            return _query_response(2)
        if url == "https://audio.example/complete.mp3":
            return httpx.Response(200, content=tiny_mp3)
        raise AssertionError(f"unexpected request: {url}")

    object_store = _MissingOnceObjectStore(tmp_path / "object-recovery-objects")
    gateway, store, call, _, _ = _durable_gateway_case(
        db_session_factory,
        tmp_path,
        handler=handler,
        profile_id="volcengine.tts.gateway-object-recovery",
        object_store=object_store,
    )
    key = call.idempotency_key
    assert key is not None

    first, first_result = gateway.invoke(call)

    assert first_result is None
    assert first.status is ProviderStatus.polling
    assert first.external_job_id == "task-object-recovery"
    assert first.error is not None
    assert first.error.code is ErrorCode.artifact_missing
    durable = store.load_by_key(key)
    assert durable is not None
    assert durable.status is ProviderStatus.polling
    assert durable.external_job_id == "task-object-recovery"

    recovered, result = gateway.invoke(call)

    assert result is not None
    assert recovered.status is ProviderStatus.succeeded
    assert result.raw_usage["task_id"] == "task-object-recovery"
    assert submit_count == 1
    assert query_count == 2
    assert store.load_by_key(key).status is ProviderStatus.succeeded


def test_async_icl2_maps_speed_to_integer_rate(tmp_path, tiny_mp3) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            return _list_api_keys_response()
        if url.endswith("/submit"):
            captured["body"] = json.loads(request.content)
            return _submit_response()
        if url.endswith("/query"):
            return _query_response(2)
        return httpx.Response(200, content=tiny_mp3)

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(
        tmp_path, options={"api_version": "v3", "resource_id": "seed-icl-2.0"}
    )
    provider.invoke_with_context(
        _call(ctx, text="你好", voice_id="S_UDXV2pG62", speed=1.5), ctx
    )

    assert captured["body"]["req_params"]["audio_params"]["speech_rate"] == 50


def test_async_icl2_task_failure_is_not_silently_retried(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            return _list_api_keys_response()
        if url.endswith("/submit"):
            return _submit_response()
        if url.endswith("/query"):
            return _query_response(3, message="InvalidSpeaker")
        raise AssertionError(f"unexpected request: {url}")

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(
        tmp_path, options={"api_version": "v3", "resource_id": "seed-icl-2.0"}
    )
    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(ctx, text="你好", voice_id="S_UDXV2pG62"), ctx
        )

    assert excinfo.value.code == ErrorCode.provider_remote_failed
    assert "InvalidSpeaker" in str(excinfo.value)


def test_async_icl2_rejects_success_without_audio_url(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "open.volcengineapi.com" in url:
            return _list_api_keys_response()
        if url.endswith("/submit"):
            return _submit_response()
        if url.endswith("/query"):
            return _query_response(2, audio_url=None)
        raise AssertionError(f"unexpected request: {url}")

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(
        tmp_path, options={"api_version": "v3", "resource_id": "seed-icl-2.0"}
    )
    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(ctx, text="你好", voice_id="S_UDXV2pG62"), ctx
        )

    assert excinfo.value.code == ErrorCode.provider_remote_failed
    assert "audio_url" in str(excinfo.value)


def test_async_icl2_http_auth_error_maps_to_auth_failed(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "open.volcengineapi.com" in str(request.url):
            return _list_api_keys_response()
        return httpx.Response(401, json={"code": 45000010, "message": "grant not found"})

    provider = VolcengineTTSProvider(_client(handler))
    ctx = _context(
        tmp_path, options={"api_version": "v3", "resource_id": "seed-icl-2.0"}
    )
    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(ctx, text="你好", voice_id="S_UDXV2pG62"), ctx
        )

    assert excinfo.value.code == ErrorCode.provider_auth_failed


def test_async_icl2_refuses_to_split_oversized_text(tmp_path) -> None:
    provider = VolcengineTTSProvider(
        _client(lambda request: pytest.fail(f"unexpected request: {request.url}"))
    )
    ctx = _context(
        tmp_path, options={"api_version": "v3", "resource_id": "seed-icl-2.0"}
    )

    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(ctx, text="字" * 100001, voice_id="S_UDXV2pG62"), ctx
        )

    assert excinfo.value.code == ErrorCode.provider_unsupported_option
    assert "will not be split" in str(excinfo.value)


@pytest.mark.parametrize(
    "options, message",
    [
        ({"format": "wav"}, "format must be mp3"),
        ({"resource_id": "seed-icl-1.0"}, "resource_id=seed-icl-2.0"),
    ],
)
def test_async_icl2_rejects_non_full_mp3_configuration(tmp_path, options, message) -> None:
    provider = VolcengineTTSProvider(
        _client(lambda request: pytest.fail(f"unexpected request: {request.url}"))
    )
    ctx = _context(tmp_path, options={"api_version": "v3", **options})

    with pytest.raises(ProviderRuntimeError) as excinfo:
        provider.invoke_with_context(
            _call(ctx, text="你好", voice_id="S_UDXV2pG62"), ctx
        )

    assert excinfo.value.code == ErrorCode.provider_unsupported_option
    assert message in str(excinfo.value)
