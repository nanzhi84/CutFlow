"""BGM / audio asset annotation: objective features + LLM semantic mood/scene.

The unified visual annotation runner (:mod:`packages.media.annotation.runner`) is
keyed on a readable *video* path and only fills portrait/b-roll semantic fields --
it physically cannot annotate a BGM asset. This module is the audio counterpart so
the must-retain '素材 AI 标注' flow covers the BGM library: it produces an
:class:`~packages.core.contracts.AnnotationV4` whose ``quality_report["bgm"]``
carries the BGM-specific semantics (mood / genre / energy / bpm / tempo_bucket /
scene_fit / avoid_scene / agent_caption) the editing-agent BGM selection consumes.

Two halves, mirroring the visual path's sensors + gated VLM split:

- **objective features** (key-free, deterministic): BPM / energy / tempo_bucket via
  ``librosa`` when it is installed, and integrated loudness (LUFS) via ffmpeg's
  ``loudnorm`` analysis pass. ``librosa`` is an OPTIONAL dependency imported lazily;
  when it is absent we skip the librosa-derived features and keep the ffmpeg LUFS
  reading -- the annotation still completes, just without bpm/energy/tempo_bucket.
- **LLM semantic** (gated, paid): an ``llm.chat`` call that infers mood / genre /
  scene_fit / avoid_scene from the objective features + the asset name. Gated behind
  a real profile + active secret exactly like the VLM path; without one the run
  DEGRADES to a sensor-only ``llm_unconfigured`` annotation and never fabricates
  semantics.

No real network in tests: the gateway and the feature extractor are injected, so a
mock gateway / mock features exercise every branch with zero IO.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.ai.gateway import ProviderCall, ProviderGateway
from packages.core.contracts import (
    AnnotationMetaV4,
    AnnotationStatus,
    AnnotationV4,
    AnnotationVersion,
    ProviderProfile,
)

logger = logging.getLogger("packages.media.annotation.bgm")

# Marker written into quality_report when no real LLM profile is configured.
LLM_UNCONFIGURED = "llm_unconfigured"
# Marker for when audio feature extraction yielded nothing usable.
FEATURES_UNAVAILABLE = "features_unavailable"

# Discrete tempo buckets (clamp LLM output / derive from BPM).
BGM_TEMPO_BUCKETS = frozenset({"slow", "mid", "fast"})
# LLM semantic fields that must be present for a COMPLETED annotation.
_REQUIRED_SEMANTIC_FIELDS = ("mood", "genre")


@dataclass
class BgmAnnotationResult:
    """Result of a gated BGM annotation run."""

    annotation: AnnotationV4
    llm_configured: bool
    provider_invocation_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Profile gating (same gate as the VLM path: real + enabled + active secret)
# ---------------------------------------------------------------------------
def resolve_llm_profile(
    gateway: ProviderGateway,
    *,
    candidate_profiles: list[ProviderProfile],
    explicit_profile: ProviderProfile | None = None,
) -> ProviderProfile | None:
    """Return a usable real ``llm.chat`` profile, or None to degrade."""
    ordered = [p for p in (explicit_profile, *candidate_profiles) if p is not None]
    seen: set[str] = set()
    for profile in ordered:
        if profile.id in seen:
            continue
        seen.add(profile.id)
        if _is_real_llm_profile(gateway, profile):
            return profile
    return None


def _is_real_llm_profile(gateway: ProviderGateway, profile: ProviderProfile) -> bool:
    if profile.capability != "llm.chat" or not profile.enabled:
        return False
    if profile.provider_id == "sandbox":
        return False
    if profile.provider_id not in gateway.plugins:
        return False
    if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
        return False
    return True


# ---------------------------------------------------------------------------
# Objective features: librosa (optional) + ffmpeg LUFS (always tried)
# ---------------------------------------------------------------------------
def extract_audio_features(audio_path: str | Path) -> dict[str, Any]:
    """Extract objective BGM features. Never raises; returns what it could measure.

    Always attempts the ffmpeg LUFS reading. When ``librosa`` is installed it adds
    BPM / energy / tempo_bucket; when it is absent those keys are simply omitted
    (the annotation still completes with the LUFS + LLM semantics). The returned
    dict's ``librosa_available`` flag records which path ran.
    """
    path = Path(audio_path)
    features: dict[str, Any] = {"librosa_available": False}
    loudness = measure_loudness_lufs(path)
    if loudness is not None:
        features["loudness_lufs"] = round(loudness, 3)

    librosa_features = _extract_librosa_features(path)
    if librosa_features is not None:
        features.update(librosa_features)
        features["librosa_available"] = True
    return features


def _extract_librosa_features(path: Path) -> dict[str, Any] | None:
    """BPM / energy / tempo_bucket via librosa, or None when unavailable/failed.

    ``librosa`` is imported lazily so this whole module imports cleanly when it is
    not installed (the must-retain feature degrades, it never crashes the runner).
    """
    try:
        import librosa  # noqa: PLC0415 - optional dependency
        import numpy as np  # noqa: PLC0415
    except Exception:  # ModuleNotFoundError or import-time failure
        logger.info("[bgm] librosa not installed; skipping objective bpm/energy features")
        return None
    if not path.exists():
        return None
    try:
        samples, sample_rate = librosa.load(str(path), sr=None, mono=True)
        if samples is None or len(samples) == 0:
            return None
        tempo, _beats = librosa.beat.beat_track(y=samples, sr=sample_rate)
        bpm = float(np.atleast_1d(tempo)[0])
        if not math.isfinite(bpm) or bpm <= 0:
            return None
        rms = librosa.feature.rms(y=samples)
        energy = max(0.0, min(1.0, float(np.mean(rms))))
    except Exception as exc:  # noqa: BLE001 - decode/analysis failure degrades
        logger.warning("[bgm] librosa feature extraction failed for %s: %s", path, exc)
        return None
    return {
        "bpm": round(bpm, 2),
        "energy": round(energy, 4),
        "tempo_bucket": _tempo_bucket(bpm),
    }


def _tempo_bucket(bpm: float) -> str:
    if bpm < 90:
        return "slow"
    if bpm < 130:
        return "mid"
    return "fast"


def _extract_loudnorm_json(output: str) -> dict | None:
    text = output or ""
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def measure_loudness_lufs(media_path: str | Path) -> float | None:
    """Integrated loudness (LUFS) via ffmpeg loudnorm analysis; None on failure."""
    from packages.media.video.ffmpeg import ffmpeg_bin

    path = Path(media_path)
    if not path.exists():
        return None
    args = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vn",
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("[bgm] loudness probe failed for %s: %s", path, exc)
        return None
    data = _extract_loudnorm_json(f"{result.stdout or ''}\n{result.stderr or ''}")
    if not data:
        return None
    try:
        loudness = float(data.get("input_i"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(loudness) or loudness <= -99:
        return None
    return loudness


# ---------------------------------------------------------------------------
# Entry: gated BGM annotation run
# ---------------------------------------------------------------------------
def annotate_bgm(
    *,
    asset_id: str,
    case_id: str,
    audio_path: str | Path,
    duration: float,
    asset_title: str = "",
    gateway: ProviderGateway,
    llm_profile: ProviderProfile | None,
    feature_extractor: Callable[[str | Path], dict[str, Any]] | None = None,
) -> BgmAnnotationResult:
    """Annotate one BGM/audio asset, gating the paid LLM path behind a real profile.

    ``feature_extractor`` is injectable so tests run without real ffmpeg / librosa.
    Without a real ``llm.chat`` profile the run DEGRADES: objective features are
    still recorded, but no semantic mood/genre is fabricated and the annotation is
    marked ``llm_unconfigured`` / failed.
    """
    extractor = feature_extractor or extract_audio_features
    try:
        features = dict(extractor(audio_path) or {})
    except Exception as exc:  # noqa: BLE001 - feature failure must not crash the run
        logger.warning("[bgm] feature extraction errored for %s: %s", asset_id, exc)
        features = {}

    if llm_profile is None:
        annotation = _degraded_annotation(
            asset_id=asset_id,
            case_id=case_id,
            duration=duration,
            features=features,
            reason=LLM_UNCONFIGURED,
        )
        return BgmAnnotationResult(annotation=annotation, llm_configured=False)

    invocation_ids: list[str] = []
    try:
        raw = _semantic_with_llm(
            gateway=gateway,
            profile=llm_profile,
            asset_id=asset_id,
            case_id=case_id,
            asset_title=asset_title,
            features=features,
            invocation_ids=invocation_ids,
        )
        semantics = _normalize_semantics(raw, features=features)
    except Exception as exc:  # noqa: BLE001 - failed semantic -> failed annotation
        logger.warning("[bgm] LLM semantic annotation failed for %s: %s", asset_id, exc)
        annotation = _failed_annotation(
            asset_id=asset_id,
            case_id=case_id,
            duration=duration,
            features=features,
            error=str(exc),
        )
        return BgmAnnotationResult(
            annotation=annotation, llm_configured=True, provider_invocation_ids=invocation_ids
        )

    annotation = _completed_annotation(
        asset_id=asset_id,
        case_id=case_id,
        duration=duration,
        features=features,
        semantics=semantics,
    )
    return BgmAnnotationResult(
        annotation=annotation, llm_configured=True, provider_invocation_ids=invocation_ids
    )


# ---------------------------------------------------------------------------
# LLM semantic call (paid, gated) + normalization
# ---------------------------------------------------------------------------
def _semantic_with_llm(
    *,
    gateway: ProviderGateway,
    profile: ProviderProfile,
    asset_id: str,
    case_id: str,
    asset_title: str,
    features: dict[str, Any],
    invocation_ids: list[str],
) -> dict[str, Any]:
    prompt = _build_semantic_prompt(asset_id=asset_id, asset_title=asset_title, features=features)
    invocation, result = gateway.invoke(
        ProviderCall(
            case_id=case_id,
            provider_profile_id=profile.id,
            capability_id="llm.chat",
            input={"prompt": prompt, "asset_id": asset_id},
            idempotency_key=f"bgm-anno-{asset_id}",
        )
    )
    invocation_ids.append(invocation.id)
    if result is None or invocation.error is not None:
        message = invocation.error.message if invocation.error else "BGM LLM provider failed."
        raise RuntimeError(message)
    content = _content_from_output(result.output)
    data = _extract_json_object(content)
    if not isinstance(data, dict):
        raise ValueError("BGM LLM annotation could not be parsed as a JSON object.")
    return data


def _build_semantic_prompt(*, asset_id: str, asset_title: str, features: dict[str, Any]) -> str:
    payload = {
        "bgm_id": asset_id,
        "bgm_name": asset_title,
        "objective_features": {
            "bpm": features.get("bpm"),
            "energy": features.get("energy"),
            "tempo_bucket": features.get("tempo_bucket"),
            "loudness_lufs": features.get("loudness_lufs"),
        },
        "required_schema": {
            "mood": "one short mood label, e.g. inspirational/tense/calm/upbeat",
            "genre": "one short genre label, e.g. ambient/edm/pop/orchestral",
            "scene_fit": ["2-6 short Chinese scenes this BGM fits"],
            "avoid_scene": ["0-4 short Chinese scenes to avoid"],
            "agent_caption": "one Chinese sentence for editing-agent BGM selection",
        },
    }
    return (
        "你是短视频 BGM 资产标注员。结合给定的客观音频特征(BPM/能量/速度桶/响度)"
        "与曲名常识推断情绪、曲风与适配场景，只返回一个合法 JSON 对象，"
        "不要输出 markdown、解释或多余文字。\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _normalize_semantics(raw: dict[str, Any], *, features: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize LLM semantics; raise when required fields are absent."""
    data = raw if isinstance(raw, dict) else {}
    for name in _REQUIRED_SEMANTIC_FIELDS:
        if not str(data.get(name) or "").strip():
            raise ValueError(f"BGM annotation missing required field: {name}")
    mood = str(data.get("mood")).strip()
    genre = str(data.get("genre")).strip()
    scene_fit = _compact_str_list(data.get("scene_fit"), 6)
    avoid_scene = _compact_str_list(data.get("avoid_scene"), 4)
    agent_caption = str(data.get("agent_caption") or "").strip()

    # tempo_bucket is objective-derived when available; otherwise accept the LLM's
    # value only if it is a legal bucket (never fabricate an illegal one).
    tempo_bucket = str(features.get("tempo_bucket") or "").strip()
    if tempo_bucket not in BGM_TEMPO_BUCKETS:
        llm_bucket = str(data.get("tempo_bucket") or "").strip().lower()
        tempo_bucket = llm_bucket if llm_bucket in BGM_TEMPO_BUCKETS else ""

    retrieval_text = " ".join(
        part for part in (mood, genre, tempo_bucket, agent_caption, *scene_fit) if part
    )
    semantics = {
        "mood": mood,
        "genre": genre,
        "scene_fit": scene_fit,
        "avoid_scene": avoid_scene,
        "agent_caption": agent_caption,
        "retrieval_text": retrieval_text,
    }
    if tempo_bucket:
        semantics["tempo_bucket"] = tempo_bucket
    return semantics


def _compact_str_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _content_from_output(output: dict[str, Any]) -> str:
    if not isinstance(output, dict):
        return ""
    for key in ("content", "text", "raw"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value
    intent = output.get("intent")
    if isinstance(intent, dict) and intent:
        return json.dumps(intent, ensure_ascii=False)
    return json.dumps(output, ensure_ascii=False)


def _extract_json_object(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# AnnotationV4 assembly (BGM semantics live in quality_report["bgm"])
# ---------------------------------------------------------------------------
def _bgm_quality_report(
    *,
    features: dict[str, Any],
    semantics: dict[str, Any] | None,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    bgm: dict[str, Any] = {
        "status": status,
        "bpm": features.get("bpm"),
        "energy": features.get("energy"),
        "tempo_bucket": features.get("tempo_bucket"),
        "loudness_lufs": features.get("loudness_lufs"),
        "librosa_available": bool(features.get("librosa_available")),
    }
    if semantics:
        bgm.update(
            {
                "mood": semantics.get("mood"),
                "genre": semantics.get("genre"),
                "scene_fit": semantics.get("scene_fit", []),
                "avoid_scene": semantics.get("avoid_scene", []),
                "agent_caption": semantics.get("agent_caption", ""),
                "retrieval_text": semantics.get("retrieval_text", ""),
            }
        )
        if semantics.get("tempo_bucket"):
            bgm["tempo_bucket"] = semantics["tempo_bucket"]
        bgm["source"] = "librosa+llm" if features.get("librosa_available") else "ffmpeg+llm"
    if error:
        bgm["error"] = error
    return {"bgm": bgm}


def _meta(asset_id: str, case_id: str, duration: float, status: AnnotationStatus) -> AnnotationMetaV4:
    return AnnotationMetaV4(
        annotation_version=AnnotationVersion.v4,
        asset_id=asset_id,
        case_id=case_id,
        material_type="bgm",
        duration=max(0.0, float(duration or 0.0)),
        annotation_status=status,
    )


def _completed_annotation(
    *,
    asset_id: str,
    case_id: str,
    duration: float,
    features: dict[str, Any],
    semantics: dict[str, Any],
) -> AnnotationV4:
    return AnnotationV4(
        meta=_meta(asset_id, case_id, duration, AnnotationStatus.completed),
        quality_report=_bgm_quality_report(features=features, semantics=semantics, status="ok"),
    )


def _degraded_annotation(
    *,
    asset_id: str,
    case_id: str,
    duration: float,
    features: dict[str, Any],
    reason: str,
) -> AnnotationV4:
    return AnnotationV4(
        meta=_meta(asset_id, case_id, duration, AnnotationStatus.failed),
        quality_report=_bgm_quality_report(features=features, semantics=None, status=reason),
    )


def _failed_annotation(
    *,
    asset_id: str,
    case_id: str,
    duration: float,
    features: dict[str, Any],
    error: str,
) -> AnnotationV4:
    return AnnotationV4(
        meta=_meta(asset_id, case_id, duration, AnnotationStatus.failed),
        quality_report=_bgm_quality_report(
            features=features, semantics=None, status="failed", error=error
        ),
    )


__all__ = [
    "BgmAnnotationResult",
    "annotate_bgm",
    "extract_audio_features",
    "measure_loudness_lufs",
    "resolve_llm_profile",
    "LLM_UNCONFIGURED",
    "FEATURES_UNAVAILABLE",
    "BGM_TEMPO_BUCKETS",
]
