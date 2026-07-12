"""Async adapters recover an in-flight vendor task by polling only (issue #193 §5).

Each case seeds a durable ``polling`` invocation (external_job_id already persisted),
then a fresh gateway call with the SAME helper key must reach the adapter's
``resume_with_context``: it polls + downloads + stores and finalizes on the shared
success path, and NEVER hits the vendor's submit/upload endpoints. Assertions track
the forbidden endpoints, not just the final status.
"""

from __future__ import annotations

import httpx
import pytest

from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderGateway,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.gateway.sqlalchemy_repository import (
    SqlAlchemyProviderInvocationStore,
    SqlAlchemyProviderRuntimeRepository,
)
from packages.ai.providers.dashscope import DashScopeASRProvider
from packages.ai.providers.runninghub import RunningHubHeyGemProvider
from packages.ai.providers.seedance import ArkSeedanceProvider
from packages.ai.providers.videoretalk import DashScopeVideoReTalkProvider
from packages.core.contracts import (
    ErrorCode,
    ProviderInvocation,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
)
from packages.core.provider_idempotency import build_provider_call_idempotency_key
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository, new_id
from packages.core.storage.secret_store import LocalSecretStore

_EXTERNAL_JOB_ID = "vendor-job-in-flight"


def _key() -> str:
    return build_provider_call_idempotency_key(
        run_id=new_id("run"),
        canonical_node_id="Node",
        logical_call_slot="slot",
        provider_profile_id="profile_1",
        input_manifest_hash="manifest_1",
    )


def _profile(*, provider_id: str, capability: str, model_id: str, secret_ref: str, options: dict) -> ProviderProfile:
    return ProviderProfile(
        id="profile_1",
        provider_id=provider_id,
        model_id=model_id,
        capability=capability,
        display_name=provider_id,
        environment="prod",
        secret_ref=secret_ref,
        timeout_sec=30,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id=f"provider.{capability}.options"),
        default_options=options,
    )


def _gateway_with_plugin(db_session_factory, tmp_path, transport, profile, plugin_cls):
    repository = Repository()
    repository.provider_profiles[profile.id] = profile
    secret_store = LocalSecretStore(tmp_path / "secrets")
    object_store = LocalObjectStore(tmp_path / "objects")
    client = httpx.Client(transport=transport)
    gateway = ProviderGateway(
        repository,
        provider_reader=SqlAlchemyProviderRuntimeRepository(db_session_factory),
        secret_store=secret_store,
        object_store=object_store,
        http_client=client,
        auto_register_real_plugins=False,
    )
    gateway.register(plugin_cls(client))
    return repository, gateway, secret_store


def _seed_polling_row(db_session_factory, *, key, profile, capability) -> SqlAlchemyProviderInvocationStore:
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    prepared = ProviderInvocation(
        id=new_id("pinv"),
        idempotency_key=key,
        provider_id=profile.provider_id,
        model_id=profile.model_id,
        provider_profile_id=profile.id,
        capability_id=capability,
        status=ProviderStatus.prepared,
    )
    store.get_or_create(prepared)
    assert store.claim_submit(prepared.id)
    store.mark_polling(prepared.id, _EXTERNAL_JOB_ID)
    row = store.load_by_key(key)
    assert row is not None and row.status is ProviderStatus.polling
    return store


def _call(key: str, capability: str) -> ProviderCall:
    return ProviderCall(
        provider_profile_id="profile_1",
        capability_id=capability,
        idempotency_key=key,
        input={"duration_sec": 3, "portrait_uri": "x", "audio_uri": "y"},
    )


def test_runninghub_heygem_resume_polls_and_stores_without_submitting(
    db_session_factory, tmp_path, media_fixture_factory
):
    video_bytes = media_fixture_factory.video(duration_sec=1.0, filename="hey.mp4").read_bytes()
    forbidden: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in ("/openapi/v2/media/upload/binary", "/task/openapi/ai-app/run", "/api/webapp/apiCallDemo"):
            forbidden.append(path)
            return httpx.Response(500)
        if path == "/task/openapi/status":
            return httpx.Response(200, json={"data": {"taskStatus": "success"}})
        if path == "/task/openapi/outputs":
            return httpx.Response(200, json={"data": {"fileUrl": "https://files.example/hey-result.mp4"}})
        if str(request.url) == "https://files.example/hey-result.mp4":
            return httpx.Response(200, content=video_bytes)
        return httpx.Response(404, text=str(request.url))

    profile = _profile(
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem",
        secret_ref="sk",
        options={"poll_interval": 0, "poll_max_attempts": 3, "video_node_id": "1", "audio_node_id": "2", "webapp_id": "w"},
    )
    repository, gateway, secret_store = _gateway_with_plugin(
        db_session_factory, tmp_path, httpx.MockTransport(handler), profile, RunningHubHeyGemProvider
    )
    profile_ref = secret_store.put("api-key")
    repository.provider_profiles["profile_1"] = profile.model_copy(update={"secret_ref": profile_ref})
    key = _key()
    store = _seed_polling_row(db_session_factory, key=key, profile=profile, capability="lipsync.video")

    invocation, result = gateway.invoke(_call(key, "lipsync.video"))

    assert forbidden == []
    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    assert invocation.external_job_id == _EXTERNAL_JOB_ID
    assert result.output["video_artifact_id"] in repository.artifacts
    assert store.load_by_key(key).status is ProviderStatus.succeeded


def test_videoretalk_resume_polls_and_stores_without_submitting(
    db_session_factory, tmp_path, media_fixture_factory
):
    video_bytes = media_fixture_factory.video(duration_sec=1.0, filename="vrt.mp4").read_bytes()
    forbidden: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/services/aigc/image2video/video-synthesis/":
            forbidden.append(path)
            return httpx.Response(500)
        if path == f"/api/v1/tasks/{_EXTERNAL_JOB_ID}":
            return httpx.Response(
                200,
                json={"output": {"task_status": "SUCCEEDED", "video_url": "https://files.example/vrt-result.mp4"}},
            )
        if str(request.url) == "https://files.example/vrt-result.mp4":
            return httpx.Response(200, content=video_bytes)
        return httpx.Response(404, text=str(request.url))

    profile = _profile(
        provider_id="dashscope.videoretalk",
        capability="lipsync.video",
        model_id="videoretalk",
        secret_ref="sk",
        options={"poll_interval": 0, "poll_max_attempts": 3},
    )
    repository, gateway, secret_store = _gateway_with_plugin(
        db_session_factory, tmp_path, httpx.MockTransport(handler), profile, DashScopeVideoReTalkProvider
    )
    profile_ref = secret_store.put("api-key")
    repository.provider_profiles["profile_1"] = profile.model_copy(update={"secret_ref": profile_ref})
    key = _key()
    store = _seed_polling_row(db_session_factory, key=key, profile=profile, capability="lipsync.video")

    invocation, result = gateway.invoke(_call(key, "lipsync.video"))

    assert forbidden == []
    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    assert result.output["video_artifact_id"] in repository.artifacts
    assert store.load_by_key(key).status is ProviderStatus.succeeded


def test_dashscope_asr_resume_polls_and_downloads_without_submitting(db_session_factory, tmp_path):
    forbidden: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/services/audio/asr/transcription":
            forbidden.append(path)
            return httpx.Response(500)
        if path == f"/api/v1/tasks/{_EXTERNAL_JOB_ID}":
            return httpx.Response(
                200,
                json={"output": {"task_status": "SUCCEEDED", "results": [{"transcription_url": "https://files.example/asr.json"}]}},
            )
        if str(request.url) == "https://files.example/asr.json":
            return httpx.Response(
                200,
                json={"transcripts": [{"text": "你好世界", "sentences": [{"begin_time": 0, "end_time": 1000, "text": "你好世界"}]}]},
            )
        return httpx.Response(404, text=str(request.url))

    profile = _profile(
        provider_id="dashscope.asr",
        capability="asr.transcribe",
        model_id="paraformer",
        secret_ref="sk",
        options={"poll_interval": 0, "poll_max_attempts": 3},
    )
    repository, gateway, secret_store = _gateway_with_plugin(
        db_session_factory, tmp_path, httpx.MockTransport(handler), profile, DashScopeASRProvider
    )
    profile_ref = secret_store.put("api-key")
    repository.provider_profiles["profile_1"] = profile.model_copy(update={"secret_ref": profile_ref})
    key = _key()
    store = _seed_polling_row(db_session_factory, key=key, profile=profile, capability="asr.transcribe")

    invocation, result = gateway.invoke(_call(key, "asr.transcribe"))

    assert forbidden == []
    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    assert "你好世界" in result.output["text"]
    assert store.load_by_key(key).status is ProviderStatus.succeeded


def test_seedance_resume_polls_and_stores_without_submitting(
    db_session_factory, tmp_path, media_fixture_factory
):
    video_bytes = media_fixture_factory.video(duration_sec=1.0, filename="seedance.mp4").read_bytes()
    tasks_path = "/api/v3/contents/generations/tasks"
    forbidden: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == tasks_path and request.method == "POST":
            forbidden.append(path)
            return httpx.Response(500)
        if path == f"{tasks_path}/{_EXTERNAL_JOB_ID}":
            return httpx.Response(
                200,
                json={
                    "id": _EXTERNAL_JOB_ID,
                    "status": "succeeded",
                    "content": {"video_url": "https://files.example/seedance.mp4"},
                    "usage": {"total_tokens": 10},
                },
            )
        if str(request.url) == "https://files.example/seedance.mp4":
            return httpx.Response(200, content=video_bytes)
        return httpx.Response(404, text=str(request.url))

    profile = _profile(
        provider_id="volcengine.seedance",
        capability="video.generate",
        model_id="doubao-seedance",
        secret_ref="sk",
        options={"base_url": "https://ark.cn-beijing.volces.com/api/v3", "poll_interval": 0, "poll_max_attempts": 3},
    )
    repository, gateway, secret_store = _gateway_with_plugin(
        db_session_factory, tmp_path, httpx.MockTransport(handler), profile, ArkSeedanceProvider
    )
    profile_ref = secret_store.put("ark-key")
    repository.provider_profiles["profile_1"] = profile.model_copy(update={"secret_ref": profile_ref})
    key = _key()
    store = _seed_polling_row(db_session_factory, key=key, profile=profile, capability="video.generate")

    invocation, result = gateway.invoke(_call(key, "video.generate"))

    assert forbidden == []
    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    assert result.output["video_artifact_id"] in repository.artifacts
    assert store.load_by_key(key).status is ProviderStatus.succeeded


class _ResumableScriptedProvider:
    """Mirrors an async adapter: mark_polling before it may crash, and a
    resume_with_context that only polls (never re-submits)."""

    provider_id = "acme.async"

    def __init__(self):
        self.submit_count = 0
        self.resume_count = 0

    def invoke_with_context(self, call, context) -> ProviderResult:
        self.submit_count += 1
        context.mark_polling("vendor-job-1")
        raise _Crash("worker died after publishing task id")

    def resume_with_context(self, call, context, external_job_id) -> ProviderResult:
        self.resume_count += 1
        assert external_job_id == "vendor-job-1"
        return ProviderResult(output={"external_job_id": external_job_id, "ok": True})


class _Crash(Exception):
    """Not a ProviderRuntimeError: models a worker dying, leaving the durable row."""


def test_crash_after_polling_recovers_by_polling_only(db_session_factory, tmp_path):
    plugin = _ResumableScriptedProvider()
    profile = _profile(
        provider_id="acme.async",
        capability="tts.speech",
        model_id="m",
        secret_ref="sk",
        options={},
    )
    repository = Repository()
    repository.provider_profiles[profile.id] = profile.model_copy(update={"secret_ref": None})
    gateway = ProviderGateway(
        repository,
        provider_reader=SqlAlchemyProviderRuntimeRepository(db_session_factory),
        object_store=LocalObjectStore(tmp_path / "objects"),
        auto_register_real_plugins=False,
    )
    gateway.register(plugin)
    key = build_provider_call_idempotency_key(
        run_id=new_id("run"),
        canonical_node_id="Tts",
        logical_call_slot="tts",
        provider_profile_id="profile_1",
        input_manifest_hash="m1",
    )
    call = ProviderCall(provider_profile_id="profile_1", capability_id="tts.speech", idempotency_key=key, input={})

    with pytest.raises(_Crash):
        gateway.invoke(call)
    assert plugin.submit_count == 1
    store = SqlAlchemyProviderInvocationStore(db_session_factory)
    assert store.load_by_key(key).status is ProviderStatus.polling

    invocation, result = gateway.invoke(call)

    # Recovery polled the in-flight task; it did NOT re-submit.
    assert plugin.submit_count == 1
    assert plugin.resume_count == 1
    assert result is not None
    assert invocation.status is ProviderStatus.succeeded
    assert store.load_by_key(key).status is ProviderStatus.succeeded


def test_resume_provider_error_is_surfaced_not_masked(db_session_factory, tmp_path):
    class _FailingResume(_ResumableScriptedProvider):
        def resume_with_context(self, call, context, external_job_id) -> ProviderResult:
            self.resume_count += 1
            raise ProviderRuntimeError(ErrorCode.provider_timeout, "poll timed out")

    plugin = _FailingResume()
    profile = _profile(
        provider_id="acme.async", capability="tts.speech", model_id="m", secret_ref="sk", options={}
    )
    repository = Repository()
    repository.provider_profiles[profile.id] = profile.model_copy(update={"secret_ref": None})
    gateway = ProviderGateway(
        repository,
        provider_reader=SqlAlchemyProviderRuntimeRepository(db_session_factory),
        object_store=LocalObjectStore(tmp_path / "objects"),
        auto_register_real_plugins=False,
    )
    gateway.register(plugin)
    key = build_provider_call_idempotency_key(
        run_id=new_id("run"),
        canonical_node_id="Tts",
        logical_call_slot="tts",
        provider_profile_id="profile_1",
        input_manifest_hash="m1",
    )
    call = ProviderCall(provider_profile_id="profile_1", capability_id="tts.speech", idempotency_key=key, input={})

    with pytest.raises(_Crash):
        gateway.invoke(call)

    invocation, result = gateway.invoke(call)

    # A resume that fails finalizes on the shared failure path (timed_out), no re-submit.
    assert plugin.submit_count == 1
    assert result is None
    assert invocation.status is ProviderStatus.timed_out
    assert SqlAlchemyProviderInvocationStore(db_session_factory).load_by_key(key).status is (
        ProviderStatus.timed_out
    )
