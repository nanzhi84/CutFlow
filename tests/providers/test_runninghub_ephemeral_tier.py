from __future__ import annotations

from types import SimpleNamespace

import httpx

from packages.ai.gateway.provider_gateway import ProviderCall
from packages.ai.providers.runninghub import RunningHubHeyGemProvider
from packages.core.contracts import ArtifactKind, ProviderOptionsSchemaRef, ProviderProfile


class RecordingContext:
    def __init__(self, profile: ProviderProfile, paths: dict[str, object]) -> None:
        self.profile = profile
        self.paths = paths
        self.polling_job_id: str | None = None
        self.store_calls: list[dict[str, object]] = []

    def get_secret(self) -> str:
        return "runninghub-key"

    def local_path_for_uri(self, uri: str):
        return self.paths[uri]

    def mark_polling(self, external_job_id: str) -> None:
        self.polling_job_id = external_job_id

    def store_media_bytes(self, **kwargs):
        self.store_calls.append(kwargs)
        return SimpleNamespace(id="art_lipsync", uri="local://cutagent-ephemeral/generated-video/result.mp4")


def test_runninghub_stores_lipsync_video_in_ephemeral_tier(media_fixture_factory):
    result_video = media_fixture_factory.video(duration_sec=1.0, filename="heygem-result.mp4")
    source_video = media_fixture_factory.video(duration_sec=1.0, filename="portrait.mp4")
    source_audio = media_fixture_factory.audio(duration_sec=1.0, filename="speech.wav")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi/v2/media/upload/binary":
            if request.content.count(b"portrait.mp4"):
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

    profile = ProviderProfile(
        id="runninghub.heygem.test",
        provider_id="runninghub.heygem",
        model_id="heygem-webapp",
        capability="lipsync.video",
        display_name="RunningHub HeyGem",
        environment="prod",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.lipsync.video.options"),
        default_options={
            "base_url": "https://www.runninghub.ai",
            "webapp_id": "webapp-1",
            "video_node_id": "video-node",
            "audio_node_id": "audio-node",
            "poll_interval": 0,
            "poll_max_attempts": 1,
        },
    )
    context = RecordingContext(
        profile,
        {
            "local://portrait.mp4": source_video,
            "local://speech.wav": source_audio,
        },
    )
    provider = RunningHubHeyGemProvider(httpx.Client(transport=httpx.MockTransport(handler)))

    result = provider.invoke_with_context(
        ProviderCall(
            case_id="case_demo",
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={
                "portrait_uri": "local://portrait.mp4",
                "audio_uri": "local://speech.wav",
                "duration_sec": 1.0,
            },
        ),
        context,
    )

    assert context.polling_job_id == "rh-job-1"
    assert result.output["video_artifact_id"] == "art_lipsync"
    assert context.store_calls[-1]["filename"] == "heygem-result.mp4"
    assert context.store_calls[-1]["kind"] == ArtifactKind.video_lipsync
    assert context.store_calls[-1]["tier"] == "ephemeral"
