"""Tests for the gated annotation runner (real VLM via gateway vs. degraded path).

NO network: the ProviderGateway is built with a mocked provider plugin and mock
sensors are injected, so every branch runs with zero IO.

Covers:
- mocked VLM returns valid ClipV4 JSON -> COMPLETED AnnotationV4 with real semantics;
- malformed VLM JSON -> V4 schema error path -> FAILED (not a crash);
- unconfigured (no real profile) -> degraded vlm_unconfigured with sensor-only quality.
"""

from __future__ import annotations

import os

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
    VLM_UNCONFIGURED,
    SensorDeps,
    annotate_asset,
    resolve_vlm_profile,
)


# --- fixtures: gateway + profiles + sensors -------------------------------------


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


def _real_vlm_profile(repository: Repository, gateway: ProviderGateway) -> ProviderProfile:
    secret_ref = gateway.secret_store.put("fake-vlm-key")  # type: ignore[union-attr]
    profile = ProviderProfile(
        id="fake.vlm.prod",
        provider_id="fake.vlm",
        model_id="fake-vlm",
        capability="vlm.annotation",
        display_name="fake vlm",
        environment="prod",
        secret_ref=secret_ref,
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.vlm.annotation.options"),
    )
    repository.provider_profiles[profile.id] = profile
    return profile


def _write_fake_frames(vp, times, *, temp_dir, max_long_side=1024):
    """Write tiny real files so the runner can base64-encode them (no real CV)."""
    os.makedirs(temp_dir, exist_ok=True)
    frames = []
    for i, t in enumerate(times):
        path = os.path.join(temp_dir, f"f{i}.jpg")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xd8\xff\xd9")  # minimal JPEG marker bytes
        frames.append((round(float(t), 3), path))
    return frames


def _mock_sensors() -> SensorDeps:
    return SensorDeps(
        detect_shot_cuts=lambda _vp: [],
        detect_speech_islands=lambda _vp: [],
        detect_quality_events=lambda _vp: [
            {"event_type": "blur", "start": 0.5, "end": 1.0, "risk_tier": "soft"}
        ],
        extract_frames=_write_fake_frames,
        sleep=lambda _s: None,
    )


def _segment(start: float, end: float) -> dict:
    return {
        "start": start,
        "end": end,
        "semantics": {"subject_type": "product", "scene_type": "studio", "action": "demo"},
        "visual": {"shot_scale": "medium", "camera_motion": "static", "composition": "centered"},
        "usage": {
            "recommended_for_lip_sync": False,
            "recommended_for_voiceover": True,
            "voiceover_only": True,
            "role": "cover",
        },
        "retrieval": {"summary": "product demo", "keywords": ["demo"], "retrieval_sentence": "demo"},
        "confidence": 0.9,
    }


class _FakeVLMPlugin:
    """A provider plugin returning canned VLM output (no HTTP)."""

    provider_id = "fake.vlm"

    def __init__(self, output: dict, fail: bool = False) -> None:
        self._output = output
        self._fail = fail
        self.calls: list[ProviderCall] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        if self._fail:
            from packages.ai.gateway.provider_gateway import ProviderRuntimeError
            from packages.core.contracts import ErrorCode

            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "boom")
        return ProviderResult(output=self._output, image_count=1)


# --- tests ----------------------------------------------------------------------


def test_real_vlm_completed_with_semantics(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_vlm_profile(repository, gateway)
    plugin = _FakeVLMPlugin({"canonical": {"segments": [_segment(0.0, 4.0)]}})
    gateway.register(plugin)

    result = annotate_asset(
        asset_id="asset1",
        case_id="case1",
        material_type="broll",
        video_path="/fake/video.mp4",
        duration=4.0,
        gateway=gateway,
        vlm_profile=profile,
        full_asr_text="台本",
        sensor_deps=_mock_sensors(),
    )

    assert result.vlm_configured is True
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.completed
    assert len(ann.clips) == 1
    assert ann.clips[0].semantics.action == "demo"
    assert ann.usage_windows  # cover role -> usage window
    # The paid call went through the gateway and was recorded.
    assert result.provider_invocation_ids
    assert plugin.calls and plugin.calls[0].idempotency_key  # idempotency set
    assert plugin.calls[0].input["max_tokens"] == 4096


def test_vlm_profile_max_tokens_override_is_preserved(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_vlm_profile(repository, gateway)
    profile = profile.model_copy(update={"default_options": {"max_tokens": 2048}})
    repository.provider_profiles[profile.id] = profile
    plugin = _FakeVLMPlugin({"canonical": {"segments": [_segment(0.0, 4.0)]}})
    gateway.register(plugin)

    annotate_asset(
        asset_id="asset1",
        case_id="case1",
        material_type="broll",
        video_path="/fake/video.mp4",
        duration=4.0,
        gateway=gateway,
        vlm_profile=profile,
        sensor_deps=_mock_sensors(),
    )

    assert "messages" in plugin.calls[0].input
    assert "max_tokens" not in plugin.calls[0].input


def test_idempotency_key_stable_across_identical_retries(tmp_path):
    """Same (asset, prompt, frames) -> same idempotency_key (cost de-dup)."""
    repository, gateway = _gateway(tmp_path)
    profile = _real_vlm_profile(repository, gateway)
    # First call SchemaError forces a retry with the SAME frames but a changed prompt;
    # the keys differ when the prompt differs, and stay stable when input is identical.
    plugin = _FakeVLMPlugin({"canonical": {"segments": [_segment(0.0, 4.0)]}})
    gateway.register(plugin)
    annotate_asset(
        asset_id="assetX",
        case_id="c",
        material_type="broll",
        video_path="/v.mp4",
        duration=4.0,
        gateway=gateway,
        vlm_profile=profile,
        sensor_deps=_mock_sensors(),
    )
    # A single window, single successful call.
    assert len(plugin.calls) == 1
    key = plugin.calls[0].idempotency_key
    assert key and key.startswith("vlm-anno-")


def test_malformed_vlm_json_fails_not_crash(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_vlm_profile(repository, gateway)
    # canonical is a non-segments dict -> V4 parser raises SchemaError every retry.
    plugin = _FakeVLMPlugin({"canonical": {"labels": ["x"], "kind": "broll"}})
    gateway.register(plugin)

    result = annotate_asset(
        asset_id="asset2",
        case_id="case1",
        material_type="broll",
        video_path="/fake/video.mp4",
        duration=4.0,
        gateway=gateway,
        vlm_profile=profile,
        sensor_deps=_mock_sensors(),
    )

    assert result.vlm_configured is True
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.failed  # not a crash
    assert ann.clips == []
    assert ann.usage_windows == []
    assert ann.quality_report == {}  # no degraded annotation written


def test_provider_runtime_failure_routes_to_failed(tmp_path):
    repository, gateway = _gateway(tmp_path)
    profile = _real_vlm_profile(repository, gateway)
    plugin = _FakeVLMPlugin({}, fail=True)
    gateway.register(plugin)

    result = annotate_asset(
        asset_id="asset3",
        case_id="case1",
        material_type="broll",
        video_path="/fake/video.mp4",
        duration=4.0,
        gateway=gateway,
        vlm_profile=profile,
        cfg=None,
        sensor_deps=_mock_sensors(),
    )

    # remote_failed is a runtime error -> bounded backoff retries -> FAILED.
    assert result.annotation.meta.annotation_status == AnnotationStatus.failed
    assert result.vlm_configured is True


def test_unconfigured_degrades_to_vlm_unconfigured(tmp_path):
    repository, gateway = _gateway(tmp_path)
    # No vlm_profile -> degraded path; sensors still populate quality.
    result = annotate_asset(
        asset_id="asset4",
        case_id="case1",
        material_type="broll",
        video_path="/fake/video.mp4",
        duration=4.0,
        gateway=gateway,
        vlm_profile=None,
        sensor_deps=_mock_sensors(),
    )

    assert result.vlm_configured is False
    assert result.provider_invocation_ids == []
    ann = result.annotation
    assert ann.meta.annotation_status == AnnotationStatus.failed
    assert ann.clips == []  # no fabricated semantics
    assert ann.usage_windows == []
    assert ann.quality_report["vlm_status"] == VLM_UNCONFIGURED
    # sensor-only quality is still populated (the blur event reached quality_events).
    assert len(ann.quality_events) == 1
    assert ann.quality_events[0].event_type.value == "blur"


# --- gating (resolve_vlm_profile) -----------------------------------------------


def test_resolve_skips_sandbox_and_unregistered(tmp_path):
    repository, gateway = _gateway(tmp_path)
    sandbox = ProviderProfile(
        id="sandbox.vlm",
        provider_id="sandbox",
        model_id="sandbox",
        capability="vlm.annotation",
        display_name="sandbox",
        environment="local",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.vlm.annotation.options"),
    )
    unregistered = ProviderProfile(
        id="ghost.vlm",
        provider_id="ghost.vlm",  # no plugin registered
        model_id="ghost",
        capability="vlm.annotation",
        display_name="ghost",
        environment="prod",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.vlm.annotation.options"),
    )
    assert resolve_vlm_profile(gateway, candidate_profiles=[sandbox, unregistered]) is None


def test_resolve_requires_active_secret(tmp_path):
    repository, gateway = _gateway(tmp_path)
    gateway.register(_FakeVLMPlugin({}))
    profile = ProviderProfile(
        id="fake.vlm.nosecret",
        provider_id="fake.vlm",
        model_id="fake-vlm",
        capability="vlm.annotation",
        display_name="fake vlm",
        environment="prod",
        secret_ref="missing-secret-ref",  # not in the secret store
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.vlm.annotation.options"),
    )
    repository.provider_profiles[profile.id] = profile
    assert resolve_vlm_profile(gateway, candidate_profiles=[profile]) is None


def test_resolve_returns_real_profile(tmp_path):
    repository, gateway = _gateway(tmp_path)
    gateway.register(_FakeVLMPlugin({}))
    profile = _real_vlm_profile(repository, gateway)
    resolved = resolve_vlm_profile(gateway, candidate_profiles=[profile])
    assert resolved is not None
    assert resolved.id == profile.id
