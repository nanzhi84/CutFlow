from __future__ import annotations

import json

import httpx

from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway, SandboxProvider
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


def _profile(repository: Repository, secret_ref: str) -> ProviderProfile:
    profile = ProviderProfile(
        id="volcengine.seedance.test",
        provider_id="volcengine.seedance",
        model_id="doubao-seedance-2-0",
        capability="video.generate",
        display_name="Seedance test",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.video.options"),
        default_options={"base_url": _BASE, "poll_interval": 0, "poll_max_attempts": 3},
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
    assert result is not None
    assert result.output["external_job_id"] == "cgt-1"
    assert result.video_seconds == 15.0
    # Seedance 2.0 param style: top-level JSON fields + a single text content.
    assert submitted_body["ratio"] == "9:16"
    assert submitted_body["resolution"] == "720p"
    assert submitted_body["duration"] == 15
    assert submitted_body["content"][0] == {"type": "text", "text": "门头特写，暖光"}
    artifact = repository.artifacts[result.output["video_artifact_id"]]
    assert artifact.media_info and artifact.media_info.media_type == "video"


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
                "references": [{"uri": "https://cdn.example/owner.jpg", "role": "reference_image"}],
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    image_entries = [c for c in submitted_body["content"] if c.get("type") == "image_url"]
    assert image_entries == [
        {"type": "image_url", "image_url": {"url": "https://cdn.example/owner.jpg"}, "role": "reference_image"}
    ]


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


def test_sandbox_supports_video_generate():
    result = SandboxProvider().invoke(
        ProviderCall(provider_profile_id="sandbox", capability_id="video.generate", input={"duration_sec": 15})
    )
    assert result.output["video_uri"].startswith("sandbox://video/seedance/")
    assert result.output["video_artifact_id"] is None
    assert result.video_seconds == 15.0
