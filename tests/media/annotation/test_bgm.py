"""Tests for the gated BGM / audio annotation path (objective features + LLM semantic).

NO network and NO real ffmpeg/librosa: the ProviderGateway is built with a mocked
provider plugin and the audio feature extractor is injected, so every branch runs
with zero IO.

Covers:
- a real ``llm.chat`` profile + valid semantics -> COMPLETED AnnotationV4 carrying
  the BGM mood/genre/scene_fit in quality_report["bgm"];
- malformed / incomplete LLM output -> FAILED (not a crash), no fabricated semantics;
- unconfigured (no real profile) -> degraded ``llm_unconfigured`` with features only;
- librosa absent -> objective bpm/energy omitted but the run still completes (the
  optional-dependency graceful-degrade contract).
"""

from __future__ import annotations

import httpx

from packages.ai.gateway import ProviderCall, ProviderGateway, ProviderResult
from packages.core.contracts import (
    AnnotationStatus,
    ProviderOptionsSchemaRef,
    ProviderProfile,
)
from packages.core.storage import Repository
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.annotation import (
    LLM_UNCONFIGURED,
    annotate_bgm,
    resolve_llm_profile,
)
from packages.media.annotation import bgm as bgm_mod


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


def _real_llm_profile(repository: Repository, gateway: ProviderGateway) -> ProviderProfile:
    secret_ref = gateway.secret_store.put("fake-llm-key")  # type: ignore[union-attr]
    profile = ProviderProfile(
        id="fake.llm.prod",
        provider_id="fake.llm",
        model_id="fake-llm",
        capability="llm.chat",
        display_name="fake llm",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.chat.options"),
    )
    repository.provider_profiles[profile.id] = profile
    return profile


class _FakeLLMPlugin:
    """A provider plugin returning canned ``llm.chat`` content (no HTTP)."""

    provider_id = "fake.llm"

    def __init__(self, content: str, fail: bool = False) -> None:
        self._content = content
        self._fail = fail
        self.calls: list[ProviderCall] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        if self._fail:
            from packages.ai.gateway.provider_gateway import ProviderRuntimeError
            from packages.core.contracts import ErrorCode

            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "boom")
        return ProviderResult(output={"content": self._content})


def _features_with_librosa(_path):
    return {
        "librosa_available": True,
        "loudness_lufs": -18.5,
        "bpm": 128.0,
        "energy": 0.42,
        "tempo_bucket": "mid",
    }


def _features_no_librosa(_path):
    # librosa absent: only the ffmpeg LUFS reading is present.
    return {"librosa_available": False, "loudness_lufs": -20.0}


_VALID_SEMANTIC_JSON = (
    '{"mood": "upbeat", "genre": "edm", '
    '"scene_fit": ["产品开箱", "促销活动"], "avoid_scene": ["悲伤回忆"], '
    '"agent_caption": "适合快节奏的开场和促销画面"}'
)


def test_bgm_completed_with_semantics(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_llm_profile(repository, gateway)
    plugin = _FakeLLMPlugin(_VALID_SEMANTIC_JSON)
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm1",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=90.0,
        asset_title="Energetic Pop",
        gateway=gateway,
        llm_profile=profile,
        feature_extractor=_features_with_librosa,
    )

    assert result.llm_configured is True
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.completed
    assert ann.meta.material_type == "bgm"
    report = ann.quality_report["bgm"]
    assert report["mood"] == "upbeat"
    assert report["genre"] == "edm"
    assert report["tempo_bucket"] == "mid"  # objective-derived
    assert report["bpm"] == 128.0
    assert "产品开箱" in report["scene_fit"]
    assert report["source"] == "librosa+llm"
    # the paid call went through the gateway and was recorded + idempotent
    assert result.provider_invocation_ids
    assert plugin.calls and plugin.calls[0].idempotency_key == "bgm-anno-bgm1"


def test_bgm_malformed_llm_output_fails_without_fabrication(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_llm_profile(repository, gateway)
    # Missing required 'genre' -> normalization raises -> FAILED, not a crash.
    plugin = _FakeLLMPlugin('{"mood": "calm"}')
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm2",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=60.0,
        gateway=gateway,
        llm_profile=profile,
        feature_extractor=_features_with_librosa,
    )

    assert result.llm_configured is True
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.failed
    report = ann.quality_report["bgm"]
    assert report["status"] == "failed"
    # objective features preserved, but no fabricated mood/genre
    assert report["bpm"] == 128.0
    assert "mood" not in report or not report.get("mood")


def test_bgm_provider_runtime_failure_fails_gracefully(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_llm_profile(repository, gateway)
    plugin = _FakeLLMPlugin("", fail=True)
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm3",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=60.0,
        gateway=gateway,
        llm_profile=profile,
        feature_extractor=_features_with_librosa,
    )

    assert result.llm_configured is True
    assert result.annotation.meta.annotation_status == AnnotationStatus.failed
    assert result.annotation.quality_report["bgm"]["status"] == "failed"
    # the failed invocation is still recorded
    assert result.provider_invocation_ids


def test_bgm_unconfigured_degrades_to_features_only(tmp_path):
    repository, gateway = _gateway(tmp_path)
    # No real llm.chat profile -> resolve_llm_profile returns None -> degrade.
    profile = resolve_llm_profile(gateway, candidate_profiles=[])
    assert profile is None

    result = annotate_bgm(
        asset_id="bgm4",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=45.0,
        gateway=gateway,
        llm_profile=profile,
        feature_extractor=_features_with_librosa,
    )

    assert result.llm_configured is False
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.failed
    report = ann.quality_report["bgm"]
    assert report["status"] == LLM_UNCONFIGURED
    # objective features still recorded; no fabricated semantics
    assert report["bpm"] == 128.0
    assert report.get("mood") in (None, "")
    assert not result.provider_invocation_ids


def test_bgm_completes_without_librosa(tmp_path):
    """librosa absent: objective bpm/energy omitted, run still COMPLETES with LUFS + LLM."""
    repository, gateway = _gateway(tmp_path)
    profile = _real_llm_profile(repository, gateway)
    plugin = _FakeLLMPlugin(_VALID_SEMANTIC_JSON)
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm5",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=90.0,
        gateway=gateway,
        llm_profile=profile,
        feature_extractor=_features_no_librosa,
    )

    assert result.annotation.meta.annotation_status == AnnotationStatus.completed
    report = result.annotation.quality_report["bgm"]
    assert report["librosa_available"] is False
    assert report["bpm"] is None
    assert report["energy"] is None
    assert report["loudness_lufs"] == -20.0
    # LLM still supplies mood/genre; source reflects the ffmpeg-only objective path
    assert report["mood"] == "upbeat"
    assert report["source"] == "ffmpeg+llm"


def test_extract_audio_features_without_librosa_omits_objective(monkeypatch, tmp_path):
    """When librosa import fails, extract_audio_features still returns (LUFS-only)."""
    # Force the lazy librosa import branch to fail and skip the real ffmpeg LUFS probe.
    monkeypatch.setattr(bgm_mod, "measure_loudness_lufs", lambda _p: None)
    monkeypatch.setattr(bgm_mod, "_extract_librosa_features", lambda _p: None)
    features = bgm_mod.extract_audio_features(tmp_path / "missing.mp3")
    assert features == {"librosa_available": False}
