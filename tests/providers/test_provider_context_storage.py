from __future__ import annotations

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import ProviderCall
from packages.core.contracts import ArtifactKind, ProviderOptionsSchemaRef, ProviderProfile
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository


class RecordingLocalObjectStore(LocalObjectStore):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.prepare_calls: list[tuple[str, str, str | None, str]] = []

    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
        tier: str = "durable",
    ):
        self.prepare_calls.append((filename, purpose, content_key, tier))
        return super().prepare_upload(filename, purpose, content_key=content_key, tier=tier)


def test_store_media_bytes_forwards_tier_to_object_store(tmp_path, media_fixture_factory):
    repository = Repository()
    object_store = RecordingLocalObjectStore(tmp_path / "objects", bucket="cutagent-ephemeral")
    profile = ProviderProfile(
        id="provider.profile",
        provider_id="provider",
        model_id="model",
        capability="tts.speech",
        display_name="Provider",
        environment="local",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
    )
    context = ProviderInvocationContext(
        repository=repository,
        profile=profile,
        invocation_id="pinv_1",
        secret_store=None,
        object_store=object_store,
    )
    call = ProviderCall(
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_1",
        provider_profile_id=profile.id,
        capability_id="tts.speech",
    )
    audio = media_fixture_factory.audio(duration_sec=1.0, filename="provider-tts.wav")

    artifact = context.store_media_bytes(
        content=audio.read_bytes(),
        filename="provider-tts.wav",
        purpose="generated-audio",
        kind=ArtifactKind.audio_tts,
        call=call,
        tier="ephemeral",
    )

    assert object_store.prepare_calls == [
        ("provider-tts.wav", "generated-audio", None, "ephemeral")
    ]
    assert artifact.uri and artifact.uri.startswith("local://cutagent-ephemeral/")
    assert artifact.media_info and artifact.media_info.media_type == "audio"
