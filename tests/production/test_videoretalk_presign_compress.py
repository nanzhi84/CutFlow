"""End-to-end tests for the real DashScope VideoReTalk fallback input path.

These drive the REAL ``DashScopeVideoReTalkProvider`` (not a mock) through the
LipSync node with internal ``local://`` portrait/audio URIs, asserting:

* the provider PRESIGNS the internal object-store URIs to public ``https://`` URLs
  before submitting to DashScope (the gap: a non-public URI is unfetchable and the
  remote call fails every time);
* an over-budget input video is COMPRESSED below the provider's hard input-size
  cap before submission (no-silent-degrade guard);
* a backing store that cannot produce a public signed URL fails loudly with a
  typed error instead of submitting a dead link.

DashScope itself is a ``httpx.MockTransport`` (no network, no key, no spend). The
object store is a local store wrapped to mimic a durable store's HTTPS presign.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway, ProviderRuntimeError
from packages.ai.providers.videoretalk import DashScopeVideoReTalkProvider
from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    MediaInfo,
    NodeRun,
    NodeStatus,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore, SignedUrlResponse, parse_local_uri, utcnow
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.assets import store_file
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


class _PresigningObjectStore(LocalObjectStore):
    """A local store whose ``signed_url`` returns a public HTTPS URL (like S3/OSS),
    mapping every object to a stable host so the DashScope mock can serve it back."""

    public_host = "https://oss.example"

    def signed_url(self, uri: str, *, expires_in: timedelta = timedelta(minutes=15)) -> SignedUrlResponse:
        ref = parse_local_uri(uri)
        return SignedUrlResponse(
            url=f"{self.public_host}/{ref.key}?sig=test",
            expires_at=utcnow() + expires_in,
            request_id="req_presign_test",
        )


def _adapter(object_store, secret_store, transport):
    repository = Repository()
    gateway = ProviderGateway(
        repository,
        secret_store=secret_store,
        object_store=object_store,
        auto_register_real_plugins=False,
        http_client=httpx.Client(transport=transport),
    )
    gateway.register(DashScopeVideoReTalkProvider(gateway.http_client))
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    adapter.provider_gateway = gateway
    return adapter, gateway


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_vrt",
        job_id="job_vrt",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_lipsync",
        run_id="run_vrt",
        node_id="LipSync",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _media_info(kind: str) -> MediaInfo:
    return MediaInfo(media_type=kind, codec="h264" if kind == "video" else "pcm", format="mp4", duration_sec=2.0)


def _real_videoretalk_profile(secret_ref: str, default_options: dict) -> ProviderProfile:
    return ProviderProfile(
        id="videoretalk.real",
        provider_id="dashscope.videoretalk",
        model_id="videoretalk",
        capability="lipsync.video",
        display_name="VideoReTalk real",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.lipsync.options"),
        default_options=default_options,
    )


def _lipsync_state(repository, object_store, factory, *, profile_id: str) -> RunState:
    portrait_file = factory.video(duration_sec=2.0, width=640, height=480, filename="portrait.mp4")
    audio_file = factory.audio(duration_sec=2.0, filename="speech.wav")
    portrait_stored = store_file(object_store, portrait_file, purpose="portrait")
    audio_stored = store_file(object_store, audio_file, purpose="audio")
    portrait = repository.create_artifact(
        kind=ArtifactKind.video_portrait_track,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        uri=portrait_stored.ref.uri,
        sha256=portrait_stored.sha256,
        media_info=_media_info("video"),
    )
    audio = repository.create_artifact(
        kind=ArtifactKind.audio_tts,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        uri=audio_stored.ref.uri,
        sha256=audio_stored.sha256,
        media_info=_media_info("audio"),
    )
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="第一句。第二句。",
        voice={"voice_id": "voice_sandbox"},
        lipsync={"enabled": True, "provider_profile_id": profile_id},
    )
    return RunState(
        request=request,
        artifacts={ArtifactKind.video_portrait_track: portrait, ArtifactKind.audio_tts: audio},
    )


def _dashscope_handler(result_video_bytes: bytes, submitted: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/services/aigc/image2video/video-synthesis/"):
            body = json.loads(request.content)
            submitted["video_url"] = body["input"]["video_url"]
            submitted["audio_url"] = body["input"]["audio_url"]
            return httpx.Response(200, json={"output": {"task_id": "vrt-1", "task_status": "PENDING"}})
        if path.endswith("/tasks/vrt-1"):
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
            return httpx.Response(200, content=result_video_bytes)
        return httpx.Response(404, text=str(request.url))

    return handler


def test_node_drives_real_videoretalk_presigning_local_inputs(
    tmp_path, media_fixture_factory, monkeypatch
):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="vrt-result.mp4")
    submitted: dict = {}
    object_store = _PresigningObjectStore(tmp_path / "objects")
    secret_store = LocalSecretStore(tmp_path / "secrets")
    adapter, gateway = _adapter(
        object_store, secret_store, httpx.MockTransport(_dashscope_handler(result_video.read_bytes(), submitted))
    )
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    secret_ref = secret_store.put("ds-key")
    adapter.repository.provider_profiles["videoretalk.real"] = _real_videoretalk_profile(
        secret_ref,
        {"base_url": "https://dashscope.aliyuncs.com/api/v1", "poll_interval": 0, "poll_max_attempts": 1},
    )

    state = _lipsync_state(
        adapter.repository, object_store, media_fixture_factory, profile_id="videoretalk.real"
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    from packages.production.pipeline import nodes

    output = nodes.lipsync.run(ctx)

    # The node fed the provider a local:// portrait/audio URI; the provider presigned
    # them to public https:// before submitting to DashScope.
    assert submitted["video_url"].startswith("https://oss.example/")
    assert submitted["audio_url"].startswith("https://oss.example/")
    report = next(a for a in output.artifacts if a.kind == ArtifactKind.lipsync_report).payload
    assert report["skipped"] is False
    assert report["provider_profile_id"] == "videoretalk.real"
    video = next(a for a in output.artifacts if a.kind == ArtifactKind.video_lipsync)
    assert video.media_info and video.media_info.media_type == "video"


def test_real_videoretalk_compresses_oversized_input_before_submit(
    tmp_path, media_fixture_factory
):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="vrt-result2.mp4")
    submitted: dict = {}
    object_store = _PresigningObjectStore(tmp_path / "objects")
    secret_store = LocalSecretStore(tmp_path / "secrets")
    repository, gateway = Repository(), None
    adapter, gateway = _adapter(
        object_store, secret_store, httpx.MockTransport(_dashscope_handler(result_video.read_bytes(), submitted))
    )
    secret_ref = secret_store.put("ds-key")
    # max_input_mb tiny -> even the small test portrait is "oversized" -> compress path.
    profile = _real_videoretalk_profile(
        secret_ref,
        {
            "base_url": "https://dashscope.aliyuncs.com/api/v1",
            "poll_interval": 0,
            "poll_max_attempts": 1,
            "max_input_mb": 0.0001,
            "compress_budget_mb": 5.0,
        },
    )
    adapter.repository.provider_profiles[profile.id] = profile

    portrait_file = media_fixture_factory.video(duration_sec=2.0, width=640, height=480, filename="portrait_big.mp4")
    portrait_stored = store_file(object_store, portrait_file, purpose="portrait")
    audio_file = media_fixture_factory.audio(duration_sec=2.0, filename="speech2.wav")
    audio_stored = store_file(object_store, audio_file, purpose="audio")

    invocation, result = gateway.invoke(
        ProviderCall(
            case_id="case_demo",
            run_id="run_vrt",
            node_run_id="nr_lipsync",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": portrait_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 2.0,
            },
        )
    )

    assert result is not None and invocation.error is None
    # The submitted video_url points at a NEWLY stored (compressed) object under the
    # generated-video purpose, NOT the original portrait object.
    assert submitted["video_url"].startswith("https://oss.example/generated-video/")
    assert "/portrait/" not in submitted["video_url"]


def test_real_videoretalk_fails_loudly_when_signed_url_not_public(
    tmp_path, media_fixture_factory
):
    """A plain LocalObjectStore signs to local:// (non-public). Submitting that to
    DashScope would fail opaquely; the provider must raise a typed error instead."""
    object_store = LocalObjectStore(tmp_path / "objects")  # signed_url returns local://
    secret_store = LocalSecretStore(tmp_path / "secrets")
    adapter, gateway = _adapter(object_store, secret_store, httpx.MockTransport(lambda r: httpx.Response(404)))
    secret_ref = secret_store.put("ds-key")
    profile = _real_videoretalk_profile(
        secret_ref,
        {"base_url": "https://dashscope.aliyuncs.com/api/v1", "poll_interval": 0, "poll_max_attempts": 1},
    )
    adapter.repository.provider_profiles[profile.id] = profile

    portrait_file = media_fixture_factory.video(duration_sec=2.0, width=320, height=240, filename="portrait_local.mp4")
    portrait_stored = store_file(object_store, portrait_file, purpose="portrait")
    audio_file = media_fixture_factory.audio(duration_sec=2.0, filename="speech3.wav")
    audio_stored = store_file(object_store, audio_file, purpose="audio")

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": portrait_stored.ref.uri,
                "audio_uri": audio_stored.ref.uri,
                "duration_sec": 2.0,
            },
        )
    )
    assert result is None
    assert invocation.error is not None
    assert invocation.error.code == ErrorCode.provider_unsupported_option
    assert "publicly fetchable" in invocation.error.message
