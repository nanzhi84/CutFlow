from __future__ import annotations

import os

import httpx
import pytest

from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway
from packages.core.contracts import ProviderOptionsSchemaRef, ProviderProfile, ProviderStatus
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.assets import store_file

pytestmark = pytest.mark.skipif(
    os.getenv("CUTAGENT_RUN_LIVE_PROVIDER_TESTS") != "1",
    reason="Set CUTAGENT_RUN_LIVE_PROVIDER_TESTS=1 to run paid live provider checks.",
)


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is required for this live provider test.")
    return value


def _gateway(tmp_path) -> tuple[Repository, ProviderGateway]:
    repository = Repository()
    gateway = ProviderGateway(
        repository,
        secret_store=LocalSecretStore(tmp_path / "secrets"),
        object_store=LocalObjectStore(tmp_path / "objects"),
        http_client=httpx.Client(timeout=180),
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
        id=f"live.{provider_id}.{capability}",
        provider_id=provider_id,
        model_id=model_id,
        capability=capability,
        display_name=f"Live {provider_id}",
        environment="prod",
        secret_ref=secret_ref,
        timeout_sec=180,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id=f"provider.{capability}.options"),
        default_options=default_options or {},
    )
    repository.provider_profiles[profile.id] = profile
    return profile


def test_live_minimax_tts_smoke(tmp_path):
    repository, gateway = _gateway(tmp_path)
    secret_ref = gateway.secret_store.put(_env("MINIMAX_API_KEY"))  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="minimax.tts",
        capability="tts.speech",
        model_id=os.getenv("MINIMAX_TTS_MODEL", "speech-02-hd"),
        secret_ref=secret_ref,
        default_options={"group_id": _env("MINIMAX_GROUP_ID")},
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"text": "这是 Cutagent live provider 验收。", "voice_id": os.getenv("MINIMAX_VOICE_ID", "male-qn-qingse")},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    artifact = repository.artifacts[result.output["audio_artifact_id"]]
    assert artifact.sha256 and artifact.media_info and artifact.media_info.media_type == "audio"


def test_live_dashscope_asr_smoke(tmp_path):
    repository, gateway = _gateway(tmp_path)
    secret_ref = gateway.secret_store.put(_env("DASHSCOPE_API_KEY"))  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.asr",
        capability="asr.transcribe",
        model_id=os.getenv("DASHSCOPE_ASR_MODEL", "paraformer-v2"),
        secret_ref=secret_ref,
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="asr.transcribe",
            input={"audio_uri": _env("CUTAGENT_LIVE_ASR_AUDIO_URI"), "language_hints": ["zh"]},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert result.output["text"]
    assert result.output["source"] == "asr"
    assert isinstance(result.output["segments"], list)
    assert result.output["segments"], "DashScope ASR should return sentence-level timestamps."
    for segment in result.output["segments"]:
        assert isinstance(segment["text"], str) and segment["text"]
        assert float(segment["end"]) >= float(segment["start"])


def test_live_dashscope_vlm_annotation_smoke(tmp_path):
    repository, gateway = _gateway(tmp_path)
    secret_ref = gateway.secret_store.put(_env("DASHSCOPE_API_KEY"))  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="dashscope.vlm",
        capability="vlm.annotation",
        model_id=os.getenv("DASHSCOPE_VLM_MODEL", "qwen-vl-max"),
        secret_ref=secret_ref,
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="vlm.annotation",
            input={
                "asset_id": "live_image",
                "asset_kind": "image",
                "asset_uri": _env("CUTAGENT_LIVE_VLM_ASSET_URI"),
                "prompt": "Return JSON with labels and quality.valid for this asset.",
            },
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert result is not None
    assert isinstance(result.output["canonical"], dict)


def test_live_runninghub_heygem_smoke(tmp_path, media_fixture_factory):
    repository, gateway = _gateway(tmp_path)
    portrait = store_file(
        gateway.object_store,
        media_fixture_factory.video(duration_sec=1.0, filename="live-portrait.mp4"),
        purpose="live-portrait",
    )
    audio = store_file(
        gateway.object_store,
        media_fixture_factory.audio(duration_sec=1.0, filename="live-speech.wav"),
        purpose="live-speech",
    )
    secret_ref = gateway.secret_store.put(_env("RUNNINGHUB_API_KEY"))  # type: ignore[union-attr]
    profile = _profile(
        repository,
        provider_id="runninghub.heygem",
        capability="lipsync.video",
        model_id="heygem-webapp",
        secret_ref=secret_ref,
        default_options={
            "webapp_id": _env("RUNNINGHUB_HEYGEM_WEBAPP_ID"),
            "video_node_id": _env("RUNNINGHUB_HEYGEM_VIDEO_NODE_ID"),
            "audio_node_id": _env("RUNNINGHUB_HEYGEM_AUDIO_NODE_ID"),
            "poll_interval": int(os.getenv("RUNNINGHUB_POLL_INTERVAL", "2")),
            "poll_max_attempts": int(os.getenv("RUNNINGHUB_POLL_MAX_ATTEMPTS", "120")),
        },
    )

    invocation, result = gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="lipsync.video",
            input={"portrait_uri": portrait.ref.uri, "audio_uri": audio.ref.uri, "duration_sec": 1.0},
        )
    )

    assert invocation.status == ProviderStatus.succeeded
    assert invocation.external_job_id
    assert result is not None
    artifact = repository.artifacts[result.output["video_artifact_id"]]
    assert artifact.sha256 and artifact.media_info and artifact.media_info.media_type == "video"
