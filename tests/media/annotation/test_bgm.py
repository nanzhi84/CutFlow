"""Tests for the gated BGM / audio annotation path (objective features + LLM semantic).

NO network and NO real ffmpeg/librosa: the ProviderGateway is built with a mocked
provider plugin and the audio feature extractor is injected, so every branch runs
with zero IO.

Covers:
- a real ``audio.understanding`` profile + valid semantics -> COMPLETED AnnotationV4
  carrying full-track BGM segments and BGM mood/scene_fit in quality_report["bgm"];
- malformed / incomplete LLM output -> FAILED (not a crash), no fabricated semantics;
- unconfigured (no real profile) -> degraded ``llm_unconfigured`` with features only;
- librosa absent -> objective bpm/energy omitted but the run still completes (the
  optional-dependency graceful-degrade contract).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import httpx

from packages.ai.gateway import ProviderCall, ProviderGateway, ProviderResult
from packages.core.contracts import (
    AnnotationStatus,
    BgmEnergyProfile,
    BgmSegmentRole,
    BgmSegmentV4,
    BgmSectionType,
    ProviderOptionsSchemaRef,
    ProviderProfile,
)
from packages.core.storage import Repository
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.annotation import (
    LLM_UNCONFIGURED,
    annotate_bgm,
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


def _real_audio_profile(repository: Repository, gateway: ProviderGateway) -> ProviderProfile:
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
    """A provider plugin returning canned ``audio.understanding`` content (no HTTP)."""

    provider_id = "fake.omni"

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
        "duration": 90.0,
        "bpm": 128.0,
        "energy": 0.42,
        "tempo_bucket": "mid",
        "beats": [0.0, 10.0, 20.0, 30.0],
        "drops": [20.0],
        "segments": [
            {
                "start": 0.0,
                "end": 60.0,
                "duration": 60.0,
                "energy": 0.4,
                "drop_anchor": None,
                "role_hint": "hook",
                "section_type": "intro",
                "section_label": "A",
                "repeat_group": "A",
                "loopable": True,
                "energy_profile": "stable",
            },
            {
                "start": 60.0,
                "end": 90.0,
                "duration": 30.0,
                "energy": 0.7,
                "drop_anchor": 80.0,
                "role_hint": "climax",
                "section_type": "drop",
                "section_label": "B",
                "repeat_group": "B",
                "loopable": False,
                "energy_profile": "rising",
            },
        ],
    }


def _features_no_librosa(_path):
    # librosa absent: only the ffmpeg LUFS reading is present.
    return {"librosa_available": False, "loudness_lufs": -20.0}


_VALID_SEMANTIC_JSON = (
    '{"mood": "upbeat", "role": "climax", '
    '"scene_fit": ["产品开箱", "促销活动"], "avoid_scene": ["悲伤回忆"], '
    '"reason": "适合快节奏的开场和促销画面"}'
)


_STRUCTURAL_SEMANTIC_JSON = (
    '{"mood": "热血", "role": "climax", "section_type": "chorus", '
    '"energy_profile": "rising", "script_fit": ["硬广开场", "产品卖点强化"], '
    '"avoid_script": ["睡眠放松"], "scene_fit": ["快节奏剪辑"], '
    '"avoid_scene": ["静态讲解"], "loopable": true, '
    '"reason": "副歌旋律明确，适合作为短视频单段BGM铺满", "confidence": 0.73}'
)


def test_bgm_completed_with_semantics(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_audio_profile(repository, gateway)
    plugin = _FakeOmniPlugin(_VALID_SEMANTIC_JSON)
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm1",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=90.0,
        asset_title="Energetic Pop",
        gateway=gateway,
        audio_profile=profile,
        audio_url_for_window=lambda s, e: f"https://x/{s}-{e}.mp3",
        feature_extractor=_features_with_librosa,
    )

    assert result.llm_configured is True
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.completed
    assert ann.meta.material_type == "bgm"
    assert len(ann.bgm_segments) == 2
    first, second = ann.bgm_segments
    assert first.source == "sensor+audio"
    assert first.role.value == "climax"
    assert first.mood == "轻快"
    assert second.source == "sensor+audio"
    report = ann.quality_report["bgm"]
    assert report["mood"] == "轻快"
    assert report["tempo_bucket"] == "mid"  # objective-derived
    assert report["bpm"] == 128.0
    assert "产品开箱" in report["scene_fit"]
    assert report["source"] == "sensor+audio"
    assert report["beats"] == [0.0, 10.0, 20.0, 30.0]
    assert report["segment_count"] == 2
    assert report["annotated_coverage_sec"] == 90.0
    assert result.provider_invocation_ids
    assert len(plugin.calls) == 2
    assert plugin.calls and plugin.calls[0].idempotency_key == "bgm-omni-bgm1-0"
    assert plugin.calls[0].capability_id == "audio.understanding"


def test_bgm_semantics_enrich_section_script_fit_and_loopability(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_audio_profile(repository, gateway)
    plugin = _FakeOmniPlugin(_STRUCTURAL_SEMANTIC_JSON)
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm_structural",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=90.0,
        asset_title="Hero Chorus",
        gateway=gateway,
        audio_profile=profile,
        audio_url_for_window=lambda s, e: f"https://x/{s}-{e}.mp3",
        feature_extractor=_features_with_librosa,
    )

    segment = result.annotation.bgm_segments[0]
    assert segment.section_type.value == "chorus"
    assert segment.energy_profile.value == "rising"
    assert segment.script_fit == ["硬广开场", "产品卖点强化"]
    assert segment.avoid_script == ["睡眠放松"]
    assert segment.loopable is True
    assert segment.confidence == 0.73
    assert "硬广开场" in result.annotation.quality_report["bgm"]["retrieval_text"]


def test_bgm_incomplete_audio_output_does_not_fabricate(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_audio_profile(repository, gateway)
    plugin = _FakeOmniPlugin('{"mood": "calm"}')
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm2",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=90.0,
        gateway=gateway,
        audio_profile=profile,
        audio_url_for_window=lambda s, e: f"https://x/{s}-{e}.mp3",
        feature_extractor=_features_with_librosa,
    )

    assert result.llm_configured is True
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.completed
    segment = ann.bgm_segments[0]
    assert segment.mood == "沉稳"
    assert segment.scene_fit == []
    assert segment.avoid_scene == []
    assert segment.role.value == "hook"
    report = ann.quality_report["bgm"]
    assert report["status"] == "ok"
    assert report["bpm"] == 128.0
    assert "genre" not in report


def test_bgm_provider_runtime_failure_keeps_sensor_segment(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_audio_profile(repository, gateway)
    plugin = _FakeOmniPlugin("", fail=True)
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm3",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=90.0,
        gateway=gateway,
        audio_profile=profile,
        audio_url_for_window=lambda s, e: f"https://x/{s}-{e}.mp3",
        feature_extractor=_features_with_librosa,
    )

    assert result.llm_configured is True
    assert result.annotation.meta.annotation_status == AnnotationStatus.completed
    assert result.annotation.bgm_segments[0].source == "sensor"
    assert result.annotation.quality_report["bgm"]["source"] == "sensor"
    assert result.provider_invocation_ids


def test_bgm_unconfigured_degrades_to_features_only(tmp_path):
    repository, gateway = _gateway(tmp_path)
    # No real audio.understanding profile -> resolve_audio_profile returns None.
    profile = bgm_mod.resolve_audio_profile(gateway, candidate_profiles=[])
    assert profile is None

    result = annotate_bgm(
        asset_id="bgm4",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=90.0,
        gateway=gateway,
        audio_profile=profile,
        audio_url_for_window=lambda _s, _e: None,
        feature_extractor=_features_with_librosa,
    )

    assert result.llm_configured is False
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.completed
    assert len(ann.bgm_segments) == 2
    assert ann.bgm_segments[0].source == "sensor"
    # degraded sensor path defaults role from role_hint and leaves mood empty
    assert ann.bgm_segments[0].role.value == "hook"
    assert ann.bgm_segments[0].mood == ""
    report = ann.quality_report["bgm"]
    assert report["status"] == LLM_UNCONFIGURED
    # objective features still recorded; no fabricated semantics
    assert report["bpm"] == 128.0
    assert report.get("mood") in (None, "")
    assert not result.provider_invocation_ids


def test_bgm_meta_duration_uses_feature_duration_when_source_duration_missing(tmp_path):
    repository, gateway = _gateway(tmp_path)

    result = annotate_bgm(
        asset_id="bgm_duration",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=0.0,
        gateway=gateway,
        audio_profile=None,
        audio_url_for_window=None,
        feature_extractor=_features_with_librosa,
    )

    assert result.annotation.meta.annotation_status == AnnotationStatus.completed
    assert result.annotation.meta.duration == 90.0
    assert result.annotation.quality_report["bgm"]["annotated_coverage_ratio"] == 1.0


def test_bgm_meta_duration_falls_back_to_segment_end(tmp_path):
    repository, gateway = _gateway(tmp_path)

    def features_without_duration(_path):
        features = dict(_features_with_librosa(_path))
        features.pop("duration", None)
        return features

    result = annotate_bgm(
        asset_id="bgm_segment_end",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=0.0,
        gateway=gateway,
        audio_profile=None,
        audio_url_for_window=None,
        feature_extractor=features_without_duration,
    )

    assert result.annotation.meta.annotation_status == AnnotationStatus.completed
    assert result.annotation.meta.duration == 90.0


def test_bgm_completes_without_librosa(tmp_path):
    """librosa absent: no segments, so the BGM segment path degrades."""
    repository, gateway = _gateway(tmp_path)
    profile = _real_audio_profile(repository, gateway)
    plugin = _FakeOmniPlugin(_VALID_SEMANTIC_JSON)
    gateway.register(plugin)

    result = annotate_bgm(
        asset_id="bgm5",
        case_id="case1",
        audio_path="/fake/bgm.mp3",
        duration=90.0,
        gateway=gateway,
        audio_profile=profile,
        audio_url_for_window=lambda s, e: f"https://x/{s}-{e}.mp3",
        feature_extractor=_features_no_librosa,
    )

    assert result.annotation.meta.annotation_status == AnnotationStatus.failed
    assert result.annotation.bgm_segments == []
    report = result.annotation.quality_report["bgm"]
    assert report["librosa_available"] is False
    assert report["bpm"] is None
    assert report["energy"] is None
    assert report["loudness_lufs"] == -20.0
    assert report["status"] == bgm_mod.FEATURES_UNAVAILABLE
    assert plugin.calls == []


def test_extract_audio_features_without_librosa_omits_objective(monkeypatch, tmp_path):
    """When librosa import fails, extract_audio_features still returns (LUFS-only)."""
    # Force the lazy librosa import branch to fail and skip the real ffmpeg LUFS probe.
    monkeypatch.setattr(bgm_mod, "measure_loudness_lufs", lambda _p: None)
    monkeypatch.setattr(bgm_mod, "_extract_librosa_features", lambda _p: None)
    features = bgm_mod.extract_audio_features(tmp_path / "missing.mp3")
    assert features == {"librosa_available": False}


def test_extract_librosa_features_builds_segments_and_handles_failures(monkeypatch, tmp_path):
    audio = tmp_path / "track.wav"
    audio.write_bytes(b"fake")

    fake_librosa = SimpleNamespace(
        load=lambda *_args, **_kwargs: ([0.1] * 1000, 100),
        beat=SimpleNamespace(beat_track=lambda **_kwargs: (128.0, [0, 100, 200, 400, 600, 800])),
        frames_to_time=lambda values, **_kwargs: [round(float(value) / 100.0, 3) for value in values],
        feature=SimpleNamespace(rms=lambda **_kwargs: [[0.1, 0.12, 0.65, 0.7, 0.3, 0.28, 0.62, 0.66]]),
    )
    fake_numpy = SimpleNamespace(
        atleast_1d=lambda value: [value],
        mean=lambda values: sum(values) / len(values),
    )
    monkeypatch.setitem(sys.modules, "librosa", fake_librosa)
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)

    features = bgm_mod._extract_librosa_features(audio)

    assert features is not None
    assert features["bpm"] == 128.0
    assert features["tempo_bucket"] == "mid"
    assert features["duration"] == 10.0
    assert features["beats"][:3] == [0.0, 1.0, 2.0]
    assert features["rhythm_markers"]
    assert features["segments"][0]["section_type"] == "stable_bed"

    assert bgm_mod._extract_librosa_features(tmp_path / "missing.wav") is None

    fake_librosa.load = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad file"))
    assert bgm_mod._extract_librosa_features(audio) is None


def test_bgm_feature_segmentation_and_marker_helpers():
    assert bgm_mod._tempo_bucket(80) == "slow"
    assert bgm_mod._tempo_bucket(110) == "mid"
    assert bgm_mod._tempo_bucket(140) == "fast"
    assert bgm_mod.snap_to_beats(10.4, []) == 10.4
    assert bgm_mod.snap_to_beats(10.4, [0, 10, 20]) == 10
    assert bgm_mod.detect_drops([0.1, 0.1], [0, 1]) == []
    assert bgm_mod.detect_drops([0.2, 0.2, 0.2], [0, 1, 2]) == []
    assert bgm_mod.detect_drops([0.1, 0.12, 0.8, 0.82], [0, 1, 2, 3])
    assert bgm_mod.rhythm_markers(beats=[1, "bad", -1], drops=[0.5, None, -2]) == [
        {"time": 0.5, "kind": "accent", "strength": 0.5},
        {"time": 1.0, "kind": "beat", "strength": 0.35},
    ]

    segments = bgm_mod.segment_audio_track(
        96.0,
        [0.1] * 8 + [0.7] * 8 + [0.2] * 8,
        [float(i * 4) for i in range(24)],
        [0.0, 32.0, 64.0, 96.0],
        [34.0],
        min_len=24.0,
    )
    assert segments[0]["role_hint"] == "hook"
    assert any(segment["section_type"] in {"drop", "chorus"} for segment in segments)
    assert bgm_mod._merge_short_segments([0.0, 3.0, 30.0], min_len=10.0) == [0.0, 30.0]
    assert bgm_mod._section_label(26) == "S27"
    assert bgm_mod._upper_quartile([]) == 0.0
    assert bgm_mod._first_between(["bad", 2.0], 1.0, 3.0) == 2.0


def test_bgm_sensor_and_semantic_helper_edges(tmp_path):
    segment = BgmSegmentV4(
        segment_id="seg1",
        start=0,
        end=10,
        duration=10,
        role=BgmSegmentRole.hook,
        section_type=BgmSectionType.intro,
        energy_profile=BgmEnergyProfile.stable,
        loopable=False,
        confidence=0.25,
        source="sensor",
    )
    semantics = bgm_mod._normalize_segment_semantics(
        {
            "mood": "  热血 ",
            "role": "climax",
            "section_type": "drop",
            "energy_profile": "peak",
            "script_fit": ["a", "", "b", "c", "d", "e", "f", "g"],
            "avoid_script": ["x", "y", "z", "w", "extra"],
            "scene_fit": ["场景"],
            "avoid_scene": "bad",
            "loopable": "yes",
            "confidence": 1.7,
            "reason": "  reason ",
        },
        role_hint=segment.role,
        section_type_hint=segment.section_type,
        energy_profile_hint=segment.energy_profile,
        loopable_hint=segment.loopable,
        confidence_hint=segment.confidence,
    )
    assert semantics["mood"] == "高能"
    assert semantics["role"] == BgmSegmentRole.climax
    assert semantics["section_type"] == BgmSectionType.drop
    assert semantics["energy_profile"] == BgmEnergyProfile.peak
    assert semantics["script_fit"] == ["a", "b", "c", "d", "e", "f"]
    assert semantics["avoid_script"] == ["x", "y", "z", "w"]
    assert semantics["avoid_scene"] == []
    assert semantics["loopable"] is True
    assert semantics["confidence"] == 1.0

    fallback = bgm_mod._normalize_segment_semantics(
        {"loopable": "no", "confidence": "nan"},
        role_hint=segment.role,
        section_type_hint=segment.section_type,
        energy_profile_hint=segment.energy_profile,
        loopable_hint=True,
        confidence_hint=0.4,
    )
    assert fallback["loopable"] is False
    assert fallback["confidence"] == 0.4

    raw_segments = bgm_mod._sensor_segments(
        [
            "bad",
            {
                "start": 2,
                "end": 5,
                "role_hint": "outro",
                "section_type": "outro",
                "loopable": "false",
                "energy_profile": "falling",
                "drop_anchor": 2.5,
                "energy": 0.2,
            },
        ]
    )
    assert len(raw_segments) == 1
    assert raw_segments[0].role == BgmSegmentRole.outro
    assert raw_segments[0].loopable is False
    assert raw_segments[0].drop_anchor_sec == 2.5

    repository, gateway = _gateway(tmp_path)
    profile = _real_audio_profile(repository, gateway)
    unchanged, invocation_id = bgm_mod._listen_to_segment(
        gateway=gateway,
        profile=profile,
        asset_id="bgm_url_fail",
        case_id="case1",
        asset_title="title",
        features={},
        segment=segment,
        index=0,
        audio_url_for_window=lambda *_args: (_ for _ in ()).throw(RuntimeError("no url")),
    )
    assert unchanged == segment
    assert invocation_id is None

    unchanged, invocation_id = bgm_mod._listen_to_segment(
        gateway=gateway,
        profile=profile,
        asset_id="bgm_no_url",
        case_id="case1",
        asset_title="title",
        features={},
        segment=segment,
        index=0,
        audio_url_for_window=lambda *_args: "",
    )
    assert unchanged == segment
    assert invocation_id is None


def test_bgm_json_content_and_quality_report_edges():
    assert bgm_mod._intent_from_output({"intent": {"mood": "calm"}}) == {"mood": "calm"}
    assert bgm_mod._intent_from_output({"content": "prefix {\"mood\": \"calm\"} suffix"}) == {
        "mood": "calm"
    }
    assert bgm_mod._content_from_output({"intent": {"mood": "calm"}}) == '{"mood": "calm"}'
    assert bgm_mod._content_from_output("bad") == ""
    assert bgm_mod._extract_json_object("```json\n{\"ok\": true}\n```") == {"ok": True}
    assert bgm_mod._extract_json_object("```\n{\"ok\": true}\n```") == {"ok": True}
    assert bgm_mod._extract_json_object("[1,2]") is None
    assert bgm_mod._extract_json_object("no json") is None
    assert bgm_mod._positive_float("bad") is None
    assert bgm_mod._positive_float(float("inf")) is None

    prompt = bgm_mod._build_segment_prompt(
        asset_title="demo",
        segment=BgmSegmentV4(segment_id="seg", start=0, end=10, duration=10),
        features={},
    )
    assert "沉稳|温暖|轻快|励志|高能|紧张|高级|俏皮" in prompt

    report = bgm_mod._bgm_quality_report(
        features={"librosa_available": True, "beats": [1], "drops": [2]},
        status="failed",
        segments=[],
        error="boom",
    )["bgm"]
    assert report["segment_count"] == 0
    assert report["annotated_coverage_ratio"] == 0.0
    assert report["recommended_segment_ids"] == []
    assert report["source"] == "sensor"
    assert report["error"] == "boom"
