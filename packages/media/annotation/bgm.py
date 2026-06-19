"""BGM / audio asset annotation: librosa-timed usage windows + gated audio listen.

The unified visual annotation runner (:mod:`packages.media.annotation.runner`) is
keyed on a readable *video* path and only fills portrait/b-roll semantic fields --
it physically cannot annotate a BGM asset. This module is the audio counterpart so
the must-retain '素材 AI 标注' flow covers the BGM library: it produces an
:class:`~packages.core.contracts.AnnotationV4` carrying ``bgm_usage_windows``
(1-3 recommended excerpts with precise seconds + role/mood/scene) plus a beat grid
in ``quality_report["bgm"]`` that the editing-agent BGM selection consumes.

Two halves, mirroring the visual path's deterministic sensors + gated semantic split
(sensors own all timestamps; the semantic model only listens, never reports seconds):

- **objective features** (key-free, deterministic): BPM / energy / tempo_bucket /
  beat grid / drops / candidate windows via ``librosa`` when it is installed, and
  integrated loudness (LUFS) via ffmpeg's ``loudnorm`` pass. ``librosa`` is an
  OPTIONAL dependency imported lazily; when it is absent there are no windows/beats
  and the annotation degrades (LUFS-only), never crashing the runner.
- **audio semantic** (gated, paid): a per-window ``audio.understanding`` call
  (Qwen-Omni) that listens to each excerpt and fills mood / scene_fit / avoid_scene /
  role / reason. Gated behind a real profile + active secret exactly like the VLM
  path; without one (or when a clip's audio URL can't be produced) the window stays
  sensor-only and no semantics are fabricated.

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
    BgmSegmentRole,
    BgmUsageWindowV4,
    ProviderProfile,
)

logger = logging.getLogger("packages.media.annotation.bgm")

# Marker written into quality_report when no real LLM profile is configured.
LLM_UNCONFIGURED = "llm_unconfigured"
# Marker for when audio feature extraction yielded nothing usable.
FEATURES_UNAVAILABLE = "features_unavailable"

# Discrete tempo buckets (derive from BPM).
BGM_TEMPO_BUCKETS = frozenset({"slow", "mid", "fast"})


@dataclass
class BgmAnnotationResult:
    """Result of a gated BGM annotation run."""

    annotation: AnnotationV4
    llm_configured: bool
    provider_invocation_ids: list[str] = field(default_factory=list)


# Profile gating (same gate as the VLM path: real + enabled + active secret)
def resolve_audio_profile(
    gateway: ProviderGateway,
    *,
    candidate_profiles: list[ProviderProfile],
    explicit_profile: ProviderProfile | None = None,
) -> ProviderProfile | None:
    """Return a usable real ``audio.understanding`` profile, or None to degrade."""
    ordered = [p for p in (explicit_profile, *candidate_profiles) if p is not None]
    seen: set[str] = set()
    for profile in ordered:
        if profile.id in seen:
            continue
        seen.add(profile.id)
        if _is_real_audio_profile(gateway, profile):
            return profile
    return None


def _is_real_audio_profile(gateway: ProviderGateway, profile: ProviderProfile) -> bool:
    if profile.capability != "audio.understanding" or not profile.enabled:
        return False
    if profile.provider_id == "sandbox":
        return False
    if profile.provider_id not in gateway.plugins:
        return False
    if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
        return False
    return True


# Objective features: librosa (optional) + ffmpeg LUFS (always tried)
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
        import librosa
        import numpy as np
    except Exception:  # ModuleNotFoundError or import-time failure
        logger.info("[bgm] librosa not installed; skipping objective bpm/energy features")
        return None
    if not path.exists():
        return None
    try:
        samples, sample_rate = librosa.load(str(path), sr=None, mono=True)
        if samples is None or len(samples) == 0:
            return None
        tempo, beat_frames = librosa.beat.beat_track(y=samples, sr=sample_rate)
        bpm = float(np.atleast_1d(tempo)[0])
        if not math.isfinite(bpm) or bpm <= 0:
            return None
        beats = [
            round(float(t), 3)
            for t in librosa.frames_to_time(beat_frames, sr=sample_rate)
        ]
        rms_frames = librosa.feature.rms(y=samples)[0]
        energy = max(0.0, min(1.0, float(np.mean(rms_frames))))
        frame_times = [
            round(float(t), 3)
            for t in librosa.frames_to_time(range(len(rms_frames)), sr=sample_rate)
        ]
        energy_curve = [max(0.0, min(1.0, float(v))) for v in rms_frames]
        duration = float(len(samples) / sample_rate)
        drops = detect_drops(energy_curve, frame_times)
        windows = candidate_windows(duration, energy_curve, frame_times, beats, drops)
    except Exception as exc:
        logger.warning("[bgm] librosa feature extraction failed for %s: %s", path, exc)
        return None
    return {
        "bpm": round(bpm, 2),
        "energy": round(energy, 4),
        "tempo_bucket": _tempo_bucket(bpm),
        "beats": beats,
        "drops": [round(d, 3) for d in drops],
        "candidate_windows": windows,
    }


def _tempo_bucket(bpm: float) -> str:
    if bpm < 90:
        return "slow"
    if bpm < 130:
        return "mid"
    return "fast"


def snap_to_beats(value: float, beats: list[float]) -> float:
    """Snap a timestamp to the nearest beat; unchanged when no beats."""
    if not beats:
        return value
    return min(beats, key=lambda b: abs(b - value))


def detect_drops(energy: list[float], times: list[float], *, z: float = 1.2) -> list[float]:
    """Time points (sec) of significant positive energy jumps (drop candidates)."""
    n = min(len(energy), len(times))
    if n < 3:
        return []
    deltas = [energy[i] - energy[i - 1] for i in range(1, n)]
    mean = sum(deltas) / len(deltas)
    var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    std = var ** 0.5
    if std <= 1e-9:
        return []
    drops: list[float] = []
    for i, d in enumerate(deltas, start=1):
        if (d - mean) / std >= z:
            drops.append(times[i])
    return drops


def candidate_windows(
    duration: float,
    energy: list[float],
    times: list[float],
    beats: list[float],
    drops: list[float],
    *,
    max_windows: int = 3,
    target_len: float = 20.0,
) -> list[dict]:
    """Pick 1-3 usable excerpt windows: drop-neighborhoods + highest-energy region.

    Deterministic. start/end snapped to nearest beats and clamped to [0, duration].
    Short tracks (<= target_len) collapse to a single whole-track window.
    """
    if duration <= 0:
        return []
    if duration <= target_len + 1e-6:
        e = _mean(energy)
        return [
            {
                "start": 0.0,
                "end": round(duration, 3),
                "energy": e,
                "drop_anchor": drops[0] if drops else None,
                "role_hint": "climax" if drops else "general",
            }
        ]

    raw: list[tuple[float, float, float | None, str]] = []
    half = target_len / 2.0
    for d in drops:
        start = max(0.0, d - half * 0.4)
        end = min(duration, start + target_len)
        raw.append((start, end, d, "climax"))
    peak_t = _peak_time(energy, times)
    if peak_t is not None:
        start = max(0.0, peak_t - half)
        end = min(duration, start + target_len)
        raw.append((start, end, None, "general"))
    if not raw:
        raw.append((0.0, min(duration, target_len), None, "hook"))

    snapped: list[dict] = []
    for start, end, anchor, hint in raw:
        s = min(snap_to_beats(start, beats), duration)
        e = min(snap_to_beats(end, beats), duration)
        if e <= s:
            e = min(duration, s + target_len)
        win = {
            "start": round(s, 3),
            "end": round(e, 3),
            "energy": _mean_between(energy, times, s, e),
            "drop_anchor": (
                round(snap_to_beats(anchor, beats), 3) if anchor is not None else None
            ),
            "role_hint": hint,
        }
        if not any(_overlaps(win, kept) for kept in snapped):
            snapped.append(win)
    snapped.sort(key=lambda w: w["energy"], reverse=True)
    return snapped[:max_windows]


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _mean_between(energy: list[float], times: list[float], start: float, end: float) -> float:
    vals = [
        energy[i]
        for i in range(min(len(energy), len(times)))
        if start <= times[i] <= end
    ]
    return _mean(vals) if vals else _mean(energy)


def _peak_time(energy: list[float], times: list[float]) -> float | None:
    n = min(len(energy), len(times))
    if n == 0:
        return None
    idx = max(range(n), key=lambda i: energy[i])
    return times[idx]


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


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


# Entry: gated BGM annotation run
def annotate_bgm(
    *,
    asset_id: str,
    case_id: str,
    audio_path: str | Path,
    duration: float,
    asset_title: str = "",
    gateway: ProviderGateway,
    audio_profile: ProviderProfile | None,
    audio_url_for_window: Callable[[float, float], str | None] | None = None,
    feature_extractor: Callable[[str | Path], dict[str, Any]] | None = None,
) -> BgmAnnotationResult:
    """Annotate one BGM/audio asset into objective windows plus optional audio semantics."""
    extractor = feature_extractor or extract_audio_features
    try:
        features = dict(extractor(audio_path) or {})
    except Exception as exc:
        logger.warning("[bgm] feature extraction errored for %s: %s", asset_id, exc)
        features = {}

    raw_windows = features.get("candidate_windows") or []
    if not raw_windows:
        annotation = _degraded_annotation(
            asset_id=asset_id,
            case_id=case_id,
            duration=duration,
            features=features,
            reason=FEATURES_UNAVAILABLE,
        )
        return BgmAnnotationResult(
            annotation=annotation,
            llm_configured=audio_profile is not None,
        )

    invocation_ids: list[str] = []
    windows = _sensor_windows(raw_windows)
    if audio_profile is not None and audio_url_for_window is not None:
        enriched: list[BgmUsageWindowV4] = []
        for index, window in enumerate(windows):
            updated, invocation_id = _listen_to_window(
                gateway=gateway,
                profile=audio_profile,
                asset_id=asset_id,
                case_id=case_id,
                asset_title=asset_title,
                features=features,
                window=window,
                index=index,
                audio_url_for_window=audio_url_for_window,
            )
            if invocation_id:
                invocation_ids.append(invocation_id)
            enriched.append(updated)
        windows = enriched

    if any(window.source == "sensor+audio" for window in windows):
        status = "ok"
    elif audio_profile is None:
        status = LLM_UNCONFIGURED
    else:
        status = "sensor"
    annotation = _annotation_with_windows(
        asset_id=asset_id,
        case_id=case_id,
        duration=duration,
        features=features,
        windows=windows,
        status=status,
    )
    return BgmAnnotationResult(
        annotation=annotation,
        llm_configured=audio_profile is not None,
        provider_invocation_ids=invocation_ids,
    )


def _sensor_windows(raw_windows: list[Any]) -> list[BgmUsageWindowV4]:
    windows: list[BgmUsageWindowV4] = []
    for index, raw in enumerate(raw_windows):
        if not isinstance(raw, dict):
            continue
        start = float(raw.get("start") or 0.0)
        end = float(raw.get("end") or 0.0)
        windows.append(
            BgmUsageWindowV4(
                segment_id=f"bgm_window_{index + 1}",
                start=start,
                end=end,
                duration=round(end - start, 3),
                role=_role_from_hint(raw.get("role_hint")),
                drop_anchor_sec=raw.get("drop_anchor"),
                energy=float(raw.get("energy") or 0.0),
                source="sensor",
            )
        )
    return windows


def _listen_to_window(
    *,
    gateway: ProviderGateway,
    profile: ProviderProfile,
    asset_id: str,
    case_id: str,
    asset_title: str,
    features: dict[str, Any],
    window: BgmUsageWindowV4,
    index: int,
    audio_url_for_window: Callable[[float, float], str | None],
) -> tuple[BgmUsageWindowV4, str | None]:
    try:
        audio_uri = audio_url_for_window(window.start, window.end)
    except Exception as exc:
        logger.warning("[bgm] audio window URL failed for %s/%s: %s", asset_id, index, exc)
        return window, None
    if not audio_uri:
        return window, None
    try:
        invocation, result = gateway.invoke(
            ProviderCall(
                case_id=case_id,
                provider_profile_id=profile.id,
                capability_id="audio.understanding",
                input={
                    "prompt": _build_window_prompt(
                        asset_title=asset_title,
                        window=window,
                        features=features,
                    ),
                    "audio_uri": audio_uri,
                    "audio_seconds": window.duration,
                    "asset_id": asset_id,
                    "segment_id": window.segment_id,
                },
                idempotency_key=f"bgm-omni-{asset_id}-{index}",
            )
        )
    except Exception as exc:
        logger.warning("[bgm] audio semantic annotation failed for %s/%s: %s", asset_id, index, exc)
        return window, None
    if result is None or invocation.error is not None:
        return window, invocation.id
    intent = _intent_from_output(result.output)
    if not intent:
        return window, invocation.id
    semantics = _normalize_window_semantics(intent, role_hint=window.role)
    return (
        window.model_copy(
            update={
                "mood": semantics["mood"],
                "scene_fit": semantics["scene_fit"],
                "avoid_scene": semantics["avoid_scene"],
                "role": semantics["role"],
                "reason": semantics["reason"],
                "source": "sensor+audio",
            }
        ),
        invocation.id,
    )


def _build_window_prompt(
    *,
    asset_title: str,
    window: BgmUsageWindowV4,
    features: dict[str, Any],
) -> str:
    payload = {
        "bgm_name": asset_title,
        "window": {
            "start": window.start,
            "end": window.end,
            "energy": window.energy,
            "has_drop": window.drop_anchor_sec is not None,
        },
        "track": {
            "bpm": features.get("bpm"),
            "tempo_bucket": features.get("tempo_bucket"),
            "loudness_lufs": features.get("loudness_lufs"),
        },
        "required_schema": {
            "mood": "一个简短情绪词",
            "role": "hook|climax|outro|general",
            "scene_fit": ["2-6 个该片段适配的中文短视频场景"],
            "avoid_scene": ["0-4 个应避免的中文场景"],
            "reason": "一句中文推荐理由",
        },
    }
    return (
        "你在听一段BGM片段。结合你听到的音乐与给定信息，推断情绪/用途/适配场景，"
        "只返回一个合法 JSON 对象，不要 markdown 或多余文字。\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _intent_from_output(output: dict[str, Any]) -> dict[str, Any]:
    intent = output.get("intent") if isinstance(output, dict) else None
    if isinstance(intent, dict):
        return intent
    content = _content_from_output(output)
    return _extract_json_object(content) or {}


def _normalize_window_semantics(
    raw: dict[str, Any],
    *,
    role_hint: BgmSegmentRole,
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    return {
        "mood": str(data.get("mood") or "").strip(),
        "scene_fit": _compact_str_list(data.get("scene_fit"), 6),
        "avoid_scene": _compact_str_list(data.get("avoid_scene"), 4),
        "role": _role_from_hint(data.get("role"), fallback=role_hint),
        "reason": str(data.get("reason") or "").strip(),
    }


def _role_from_hint(
    value: Any,
    *,
    fallback: BgmSegmentRole = BgmSegmentRole.general,
) -> BgmSegmentRole:
    text = str(value or "").strip().lower()
    if text in {role.value for role in BgmSegmentRole}:
        return BgmSegmentRole(text)
    return fallback


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


# AnnotationV4 assembly (BGM semantics live in quality_report["bgm"])
def _bgm_quality_report(
    *,
    features: dict[str, Any],
    status: str,
    windows: list[BgmUsageWindowV4] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    bgm: dict[str, Any] = {
        "status": status,
        "bpm": features.get("bpm"),
        "energy": features.get("energy"),
        "tempo_bucket": features.get("tempo_bucket"),
        "loudness_lufs": features.get("loudness_lufs"),
        "librosa_available": bool(features.get("librosa_available")),
        "beats": features.get("beats") or [],
        "drops": features.get("drops") or [],
    }
    if windows is not None:
        bgm["candidate_window_count"] = len(windows)
        bgm["source"] = (
            "sensor+audio" if any(w.source == "sensor+audio" for w in windows) else "sensor"
        )
        semantic_window = next((w for w in windows if w.source == "sensor+audio"), None)
        if semantic_window is not None:
            bgm.update(
                {
                    "mood": semantic_window.mood,
                    "scene_fit": semantic_window.scene_fit,
                    "avoid_scene": semantic_window.avoid_scene,
                    "retrieval_text": " ".join(
                        part
                        for part in (
                            semantic_window.mood,
                            semantic_window.reason,
                            *semantic_window.scene_fit,
                        )
                        if part
                    ),
                }
            )
    if error:
        bgm["error"] = error
    return {"bgm": bgm}


def _meta(
    asset_id: str,
    case_id: str,
    duration: float,
    status: AnnotationStatus,
) -> AnnotationMetaV4:
    return AnnotationMetaV4(
        annotation_version=AnnotationVersion.v4,
        asset_id=asset_id,
        case_id=case_id,
        material_type="bgm",
        duration=max(0.0, float(duration or 0.0)),
        annotation_status=status,
    )


def _annotation_with_windows(
    *,
    asset_id: str,
    case_id: str,
    duration: float,
    features: dict[str, Any],
    windows: list[BgmUsageWindowV4],
    status: str,
) -> AnnotationV4:
    return AnnotationV4(
        meta=_meta(asset_id, case_id, duration, AnnotationStatus.completed),
        bgm_usage_windows=windows,
        quality_report=_bgm_quality_report(
            features=features,
            windows=windows,
            status=status,
        ),
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
        quality_report=_bgm_quality_report(features=features, status=reason),
    )


__all__ = [
    "BgmAnnotationResult",
    "annotate_bgm",
    "extract_audio_features",
    "measure_loudness_lufs",
    "resolve_audio_profile",
    "LLM_UNCONFIGURED",
    "FEATURES_UNAVAILABLE",
    "BGM_TEMPO_BUCKETS",
]
