"""Gated annotation runner: analyze a b-roll/portrait asset -> AnnotationV4.

This is the wiring layer that the b-roll analyzer service / asset-annotation flow
calls. It bridges the pure pipeline (:mod:`packages.media.annotation.pipeline`) to
the real world:

- when a REAL ``vlm.annotation`` provider profile + active secret exist, it builds a
  :class:`~packages.media.annotation.pipeline.V4Deps` whose ``vlm_call`` goes through
  the :class:`~packages.ai.gateway.ProviderGateway` (paid path), and runs the full
  classified-retry pipeline -> a COMPLETED / FAILED ``AnnotationV4`` with real semantics;
- otherwise it DEGRADES gracefully: it still runs the deterministic sensors + window
  plan + quality report, but returns an annotation with ``meta.annotation_status=failed``
  carrying ``quality_report["vlm_status"] = "vlm_unconfigured"``, empty ``clips``, and
  the sensor-only quality report - it NEVER fabricates semantics.

Cost discipline: the VLM call is paid, so the gateway-backed ``vlm_call`` sets a
``ProviderCall.idempotency_key`` per (asset, window) and the pipeline bounds retries
by failure class (see ``V4Config`` caps) - failures never multiply calls unboundedly.

No real network in tests: the gateway is injected, so a mock gateway / mock sensors
exercise every branch with zero IO.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from packages.ai.gateway import ProviderCall, ProviderGateway
from packages.core.contracts import (
    AnnotationMetaV4,
    AnnotationStatus,
    AnnotationV4,
    AnnotationVersion,
    ErrorCode,
    ProviderProfile,
)

from . import _assemble as assemble
from . import sensors
from .errors import RuntimeVLMError, UnrecoverableError
from .pipeline import V4Config, V4Deps, run_annotation_v4
from .report import build_quality_report

logger = logging.getLogger("packages.media.annotation.runner")

# Degraded status marker written into quality_report when no real VLM is configured.
VLM_UNCONFIGURED = "vlm_unconfigured"

# Gateway error codes that the pipeline should retry with backoff (transient).
_RUNTIME_ERROR_CODES = {
    ErrorCode.provider_timeout,
    ErrorCode.provider_remote_failed,
    ErrorCode.provider_quota_exceeded,
}

@dataclass
class GatedAnnotationResult:
    """Result of a gated run: the AnnotationV4 plus whether the real VLM path ran."""

    annotation: AnnotationV4
    vlm_configured: bool
    provider_invocation_ids: list[str]


# Profile gating: real provider only when enabled and backed by an active secret.
def resolve_vlm_profile(
    gateway: ProviderGateway,
    *,
    candidate_profiles: list[ProviderProfile],
    explicit_profile: ProviderProfile | None = None,
) -> ProviderProfile | None:
    """Return a usable real ``vlm.annotation`` profile, or None to degrade.

    A profile is usable only when it is enabled, its provider plugin is registered,
    it is NOT the sandbox provider, and its secret (if any) is active. This is the
    same gate the pipeline nodes use (``ProviderProfileResolver.first_available``).
    """
    ordered = [p for p in (explicit_profile, *candidate_profiles) if p is not None]
    seen: set[str] = set()
    for profile in ordered:
        if profile.id in seen:
            continue
        seen.add(profile.id)
        if _is_real_vlm_profile(gateway, profile):
            return profile
    return None


def _is_real_vlm_profile(gateway: ProviderGateway, profile: ProviderProfile) -> bool:
    if profile.capability != "vlm.annotation" or not profile.enabled:
        return False
    if profile.provider_id == "sandbox":
        return False
    if profile.provider_id not in gateway.plugins:
        return False
    if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
        return False
    return True


# Entry: gated annotation run
def annotate_asset(
    *,
    asset_id: str,
    case_id: str,
    material_type: str,
    video_path: str,
    duration: float,
    gateway: ProviderGateway,
    vlm_profile: ProviderProfile | None,
    full_asr_text: str = "",
    cfg: V4Config | None = None,
    sensor_deps: SensorDeps | None = None,
) -> GatedAnnotationResult:
    """Annotate one asset, gating the paid VLM path behind a real profile + secret.

    Args:
        vlm_profile: a real ``vlm.annotation`` profile (already gated by the caller via
            :func:`resolve_vlm_profile`), or None to take the degraded path.
        sensor_deps: deterministic sensor callables (defaults to the real CV sensors);
            tests inject mocks so no ffmpeg/scenedetect/silero runs.
    """
    cfg = cfg or V4Config()
    sensor_deps = sensor_deps or SensorDeps.real()

    if vlm_profile is None:
        annotation = _degraded_annotation(
            asset_id=asset_id,
            case_id=case_id,
            material_type=material_type,
            video_path=video_path,
            duration=duration,
            cfg=cfg,
            sensor_deps=sensor_deps,
        )
        return GatedAnnotationResult(
            annotation=annotation, vlm_configured=False, provider_invocation_ids=[]
        )

    invocation_ids: list[str] = []
    vlm_call = _gateway_vlm_call(
        gateway=gateway,
        profile=vlm_profile,
        asset_id=asset_id,
        case_id=case_id,
        invocation_ids=invocation_ids,
    )
    deps = V4Deps(
        detect_shot_cuts=sensor_deps.detect_shot_cuts,
        detect_speech_islands=sensor_deps.detect_speech_islands,
        detect_quality_events=sensor_deps.detect_quality_events,
        extract_frames=sensor_deps.extract_frames,
        vlm_call=vlm_call,
        resolve_asr_text=lambda _vp: full_asr_text or "",
        sleep=sensor_deps.sleep,
        detect_max_faces=sensor_deps.detect_max_faces,
    )
    annotation = run_annotation_v4(
        asset_id=asset_id,
        case_id=case_id,
        material_type=material_type,
        video_path=video_path,
        duration=duration,
        deps=deps,
        cfg=cfg,
    )
    return GatedAnnotationResult(
        annotation=annotation, vlm_configured=True, provider_invocation_ids=invocation_ids
    )


# Gateway-backed vlm_call (paid path, idempotent, bounded by pipeline retries)
def _gateway_vlm_call(
    *,
    gateway: ProviderGateway,
    profile: ProviderProfile,
    asset_id: str,
    case_id: str,
    invocation_ids: list[str],
) -> Callable[[str, list[tuple[float, str]]], str]:
    """Build the synchronous ``vlm_call`` dep that invokes the gateway once per call.

    - frames are read + base64-encoded into an OpenAI-style multimodal ``messages``
      payload (matching :class:`~packages.ai.providers.dashscope.DashScopeVLMProvider`);
    - a deterministic ``idempotency_key`` per (asset, prompt, frames) lets the gateway /
      provider de-duplicate the paid call across pipeline retries that resend the same input;
    - gateway transient failures (timeout / remote_failed / quota) -> RuntimeVLMError
      (pipeline backs off); other gateway failures -> UnrecoverableError (no retry);
    - the parsed canonical JSON is re-serialized so the V4 parser sees a JSON string.
    """

    def _call(prompt: str, frames: list[tuple[float, str]]) -> str:
        content = _build_multimodal_content(prompt, frames)
        idem = _idempotency_key(asset_id, prompt, frames)
        input_payload: dict[str, Any] = {"messages": content}
        invocation, result = gateway.invoke(
            ProviderCall(
                case_id=case_id,
                provider_profile_id=profile.id,
                capability_id="vlm.annotation",
                input=input_payload,
                idempotency_key=idem,
            )
        )
        invocation_ids.append(invocation.id)
        if result is None or invocation.error is not None:
            code = invocation.error.code if invocation.error else ErrorCode.provider_remote_failed
            message = invocation.error.message if invocation.error else "VLM provider failed."
            if code in _RUNTIME_ERROR_CODES:
                raise RuntimeVLMError(message)
            raise UnrecoverableError(message)
        return _result_to_json_string(result.output)

    return _call


def _build_multimodal_content(prompt: str, frames: list[tuple[float, str]]) -> list[dict[str, Any]]:
    """OpenAI-style multimodal message: prompt text + each frame as a base64 data URL."""
    parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for time_sec, frame_path in frames:
        try:
            with open(frame_path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode()
        except OSError as exc:
            # A frame that vanished mid-run is unrecoverable for this window's call.
            raise UnrecoverableError(f"frame not readable: {frame_path} ({exc})") from exc
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
        )
        parts.append({"type": "text", "text": f"时间点 {float(time_sec):.2f}s"})
    return [{"role": "user", "content": parts}]


def _idempotency_key(asset_id: str, prompt: str, frames: list[tuple[float, str]]) -> str:
    """Deterministic key over (asset, prompt, frame times) so identical retries de-dup."""
    digest = hashlib.sha256()
    digest.update(asset_id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(prompt.encode("utf-8"))
    for time_sec, _path in frames:
        digest.update(f"\x00{float(time_sec):.3f}".encode("utf-8"))
    return f"vlm-anno-{digest.hexdigest()[:24]}"


def _result_to_json_string(output: dict[str, Any]) -> str:
    """Re-serialize the gateway VLM output into a JSON string for the V4 parser.

    The DashScope VLM provider returns ``{"canonical": {...}}`` (already parsed); the
    V4 parser wants a JSON string of the segments object. Fall back to common keys.
    """
    canonical = output.get("canonical")
    if isinstance(canonical, dict) and canonical:
        return json.dumps(canonical, ensure_ascii=False)
    for key in ("content", "text", "raw"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(output, ensure_ascii=False)


# Degraded path (no real VLM): sensor-only quality, empty semantics, vlm_unconfigured
def _degraded_annotation(
    *,
    asset_id: str,
    case_id: str,
    material_type: str,
    video_path: str,
    duration: float,
    cfg: V4Config,
    sensor_deps: SensorDeps,
) -> AnnotationV4:
    """Run sensors only -> a FAILED annotation marked ``vlm_unconfigured`` (no fabrication).

    Clips/usage_windows stay empty (those need the VLM's semantics); the deterministic
    sensor-only quality report is still populated so the operator sees real CV signals.
    """
    try:
        cv_events = list((sensor_deps.detect_quality_events(video_path)) or [])
    except Exception as exc:
        logger.warning("[annotation] degraded sensor pass error, no events: %s", exc)
        cv_events = []
    quality_events = assemble.assemble_quality_events(cv_events, duration)
    quality_report = build_quality_report(
        material_type=material_type,
        duration=duration,
        clips=[],
        quality_events=quality_events,
    )
    quality_report = dict(quality_report)
    quality_report["vlm_status"] = VLM_UNCONFIGURED

    meta = AnnotationMetaV4(
        annotation_version=AnnotationVersion.v4,
        asset_id=asset_id,
        case_id=case_id,
        material_type=material_type,
        duration=max(0.0, float(duration or 0.0)),
        annotation_status=AnnotationStatus.failed,
    )
    return AnnotationV4(
        meta=meta,
        clips=[],
        quality_events=quality_events,
        quality_report=quality_report,
        usage_windows=[],
        evidence_frames=[],
    )


# Sensor dependency bundle (real CV sensors by default; tests inject mocks)
@dataclass
class SensorDeps:
    """Deterministic sensor callables the runner feeds into the pipeline.

    ``real()`` binds the pure-CV sensor suite. Tests construct a mock instance so
    no ffmpeg / scenedetect / silero / opencv runs.
    """

    detect_shot_cuts: Callable[[str], list[float]]
    detect_speech_islands: Callable[[str], list]
    detect_quality_events: Callable[[str], list[dict]]
    extract_frames: Callable[..., list[tuple[float, str]]]
    sleep: Callable[[float], None] = staticmethod(lambda _s: None)
    # Deterministic multi-face sensor over a window's frame paths (portrait gating).
    detect_max_faces: Callable[[list[str]], int] = staticmethod(
        lambda paths: int(sensors.max_faces_in_frame_paths(list(paths or [])))
    )

    @classmethod
    def real(cls) -> SensorDeps:
        import time

        def _detect_quality_events(video_path: str) -> list[dict]:
            return list(sensors.detect_cv_quality_events(video_path) or []) + list(
                sensors.detect_motion_events(video_path) or []
            )

        def _detect_speech_islands(video_path: str) -> list:
            return list(sensors.detect_speech_islands(video_path) or [])

        def _extract_frames(
            video_path: str,
            sample_times: list[float],
            *,
            temp_dir: str,
            max_long_side: int = 1024,
        ) -> list[tuple[float, str]]:
            return sensors.extract_frames_for_times(
                video_path, sample_times, temp_dir=temp_dir, max_long_side=max_long_side
            )

        return cls(
            detect_shot_cuts=lambda vp: list(sensors.detect_shot_cuts(vp) or []),
            detect_speech_islands=_detect_speech_islands,
            detect_quality_events=_detect_quality_events,
            extract_frames=_extract_frames,
            sleep=lambda seconds: time.sleep(max(0.0, float(seconds))),
            detect_max_faces=lambda paths: int(
                sensors.max_faces_in_frame_paths(list(paths or []))
            ),
        )
