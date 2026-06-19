from __future__ import annotations

import httpx

from packages.ai.gateway import ProviderCall, ProviderGateway, ProviderResult
from packages.core import contracts as c
from packages.core.contracts import ProviderOptionsSchemaRef, ProviderProfile
from packages.core.storage import Repository
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.annotation.bgm import annotate_bgm


FEATURES = {
    "librosa_available": True,
    "bpm": 128.0,
    "energy": 0.6,
    "tempo_bucket": "fast",
    "loudness_lufs": -14.0,
    "beats": [0.0, 0.5, 45.0, 58.0, 75.0],
    "drops": [58.0],
    "candidate_windows": [
        {
            "start": 45.0,
            "end": 75.0,
            "energy": 0.8,
            "drop_anchor": 58.0,
            "role_hint": "climax",
        },
    ],
}


def _gateway(tmp_path) -> tuple[Repository, ProviderGateway]:
    repository = Repository()
    gateway = ProviderGateway(
        repository,
        secret_store=LocalSecretStore(tmp_path / "secrets"),
        object_store=LocalObjectStore(tmp_path / "objects"),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        auto_register_real_plugins=False,
    )
    return repository, gateway


def _audio_profile(repository: Repository, gateway: ProviderGateway) -> ProviderProfile:
    secret_ref = gateway.secret_store.put("fake-omni-key")  # type: ignore[union-attr]
    profile = ProviderProfile(
        id="fake.omni.prod",
        provider_id="fake.omni",
        model_id="fake-omni",
        capability="audio.understanding",
        display_name="fake omni",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.audio.options"),
    )
    repository.provider_profiles[profile.id] = profile
    return profile


class _FakeOmniPlugin:
    provider_id = "fake.omni"

    def __init__(self, intent: dict, fail: bool = False) -> None:
        self.intent = intent
        self.fail = fail
        self.calls: list[ProviderCall] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        if self.fail:
            from packages.ai.gateway.provider_gateway import ProviderRuntimeError
            from packages.core.contracts import ErrorCode

            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "boom")
        return ProviderResult(output={"intent": self.intent})


def test_annotate_bgm_full_listen_produces_typed_windows(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _audio_profile(repository, gateway)
    plugin = _FakeOmniPlugin(
        {
            "mood": "燃",
            "scene_fit": ["高光"],
            "avoid_scene": [],
            "role": "climax",
            "reason": "副歌",
        }
    )
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="a",
        case_id="c",
        audio_path="x.mp3",
        duration=80.0,
        asset_title="一马当先",
        gateway=gateway,
        audio_profile=profile,
        audio_url_for_window=lambda s, e: f"https://x/{s}-{e}.mp3",
        feature_extractor=lambda _p: dict(FEATURES),
    )

    ann = result.annotation
    assert ann.meta.material_type == "bgm"
    assert len(ann.bgm_usage_windows) == 1
    window = ann.bgm_usage_windows[0]
    assert window.start == 45.0
    assert window.end == 75.0
    assert window.drop_anchor_sec == 58.0
    assert window.role == c.BgmSegmentRole.climax
    assert window.mood == "燃"
    assert window.scene_fit == ["高光"]
    assert window.source == "sensor+audio"
    assert ann.quality_report["bgm"]["beats"] == FEATURES["beats"]
    assert ann.quality_report["bgm"]["drops"] == FEATURES["drops"]
    assert plugin.calls[0].capability_id == "audio.understanding"
    assert plugin.calls[0].input["audio_uri"] == "https://x/45.0-75.0.mp3"


def test_annotate_bgm_no_audio_profile_degrades_to_sensor(tmp_path):
    _repository, gateway = _gateway(tmp_path)

    result = annotate_bgm(
        asset_id="a",
        case_id="c",
        audio_path="x.mp3",
        duration=80.0,
        asset_title="t",
        gateway=gateway,
        audio_profile=None,
        audio_url_for_window=lambda _s, _e: None,
        feature_extractor=lambda _p: dict(FEATURES),
    )

    ann = result.annotation
    assert result.llm_configured is False
    assert len(ann.bgm_usage_windows) == 1
    window = ann.bgm_usage_windows[0]
    assert window.source == "sensor"
    assert window.mood == ""
    assert window.role == c.BgmSegmentRole.climax
    assert ann.quality_report["bgm"]["beats"] == FEATURES["beats"]


def test_annotate_bgm_no_librosa_degrades_without_windows(tmp_path):
    _repository, gateway = _gateway(tmp_path)

    result = annotate_bgm(
        asset_id="a",
        case_id="c",
        audio_path="x.mp3",
        duration=0.0,
        asset_title="t",
        gateway=gateway,
        audio_profile=None,
        feature_extractor=lambda _p: {"librosa_available": False, "loudness_lufs": -14.0},
    )

    ann = result.annotation
    assert ann.bgm_usage_windows == []
    assert ann.meta.annotation_status == c.AnnotationStatus.failed
