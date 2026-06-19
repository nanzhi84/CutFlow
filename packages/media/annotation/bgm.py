"""BGM / audio asset annotation: librosa full-track segments + gated audio listen.

The unified visual annotation runner (:mod:`packages.media.annotation.runner`) is
keyed on a readable *video* path and only fills portrait/b-roll semantic fields --
it physically cannot annotate a BGM asset. This module is the audio counterpart so
the must-retain '素材 AI 标注' flow covers the BGM library: it produces an
:class:`~packages.core.contracts.AnnotationV4` carrying ``bgm_segments``
(contiguous full-track segments with precise seconds + role/mood/scene) plus a
beat grid in ``quality_report["bgm"]`` that BGM selection consumes.

Two halves, mirroring the visual path's deterministic sensors + gated semantic split
(sensors own all timestamps; the semantic model only listens, never reports seconds):

- **objective features** (key-free, deterministic): BPM / energy / tempo_bucket /
  beat grid / drops / full-track segments via ``librosa`` when it is installed, and
  integrated loudness (LUFS) via ffmpeg's ``loudnorm`` pass. ``librosa`` is an
  OPTIONAL dependency imported lazily; when it is absent there are no windows/beats
  and the annotation degrades (LUFS-only), never crashing the runner.
- **audio semantic** (gated, paid): a per-segment ``audio.understanding`` call
  (Qwen-Omni) that listens to each segment and fills mood / scene_fit / avoid_scene /
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
    BgmSegmentV4,
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
        detected_bpm = float(np.atleast_1d(tempo)[0])
        bpm = detected_bpm if math.isfinite(detected_bpm) and detected_bpm > 0 else None
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
        segments = segment_audio_track(duration, energy_curve, frame_times, beats, drops)
    except Exception as exc:
        logger.warning("[bgm] librosa feature extraction failed for %s: %s", path, exc)
        return None
    features = {
        "energy": round(energy, 4),
        "beats": beats,
        "drops": [round(d, 3) for d in drops],
        "segments": segments,
    }
    if bpm is not None:
        features["bpm"] = round(bpm, 2)
        features["tempo_bucket"] = _tempo_bucket(bpm)
    return features


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


def segment_audio_track(
    duration: float,
    energy: list[float],
    times: list[float],
    beats: list[float],
    drops: list[float],
    *,
    min_len: float = 24.0,
    target_len: float = 60.0,
    max_len: float = 90.0,
) -> list[dict]:
    """Split the full BGM track into contiguous, beat-snapped segments."""
    if duration <= 0:
        return []
    if duration <= target_len + 10.0:
        anchor = _first_between(drops, 0.0, duration)
        return [
            {
                "start": 0.0,
                "end": round(duration, 3),
                "duration": round(duration, 3),
                "energy": _mean(energy),
                "drop_anchor": round(snap_to_beats(anchor, beats), 3) if anchor is not None else None,
                "role_hint": "hook",
            }
        ]

    boundaries = _segment_boundaries(
        duration,
        beats,
        min_len=min_len,
        target_len=target_len,
        max_len=max_len,
    )
    segments: list[dict] = []
    raw_energies: list[float] = []
    for start, end in zip(boundaries, boundaries[1:]):
        start = round(max(0.0, min(duration, start)), 3)
        end = round(max(start, min(duration, end)), 3)
        if end <= start:
            continue
        raw_energies.append(_mean_between(energy, times, start, end))
        anchor = _first_between(drops, start, end)
        segments.append(
            {
                "start": start,
                "end": end,
                "duration": round(end - start, 3),
                "energy": raw_energies[-1],
                "drop_anchor": round(snap_to_beats(anchor, beats), 3) if anchor is not None else None,
                "role_hint": "general",
            }
        )
    if not segments:
        return []

    high_energy_threshold = _upper_quartile(raw_energies)
    for index, segment in enumerate(segments):
        is_first = index == 0
        is_last = index == len(segments) - 1
        if is_first:
            role = "hook"
        elif is_last and float(segment["energy"]) < high_energy_threshold:
            role = "outro"
        elif segment["drop_anchor"] is not None or float(segment["energy"]) >= high_energy_threshold:
            role = "climax"
        else:
            role = "general"
        segment["role_hint"] = role
    return segments


def _segment_boundaries(
    duration: float,
    beats: list[float],
    *,
    min_len: float,
    target_len: float,
    max_len: float,
) -> list[float]:
    clean_beats = sorted({round(float(b), 3) for b in beats if 0 < float(b) < duration})
    boundaries = [0.0]
    if len(clean_beats) >= 2:
        intervals = [
            clean_beats[i] - clean_beats[i - 1]
            for i in range(1, len(clean_beats))
            if clean_beats[i] > clean_beats[i - 1]
        ]
        beat_interval = _median(intervals) if intervals else 0.0
        if beat_interval > 0:
            beats_per_segment = max(8, round(target_len / beat_interval / 8) * 8)
            for idx in range(beats_per_segment - 1, len(clean_beats), beats_per_segment):
                boundary = clean_beats[idx]
                if min_len <= boundary <= duration - min_len:
                    boundaries.append(boundary)
    if len(boundaries) == 1:
        boundary = target_len
        while boundary <= duration - min_len:
            boundaries.append(round(boundary, 3))
            boundary += target_len
    boundaries.append(round(duration, 3))
    boundaries = sorted({round(b, 3) for b in boundaries})
    boundaries = _merge_short_segments(boundaries, min_len=min_len)
    boundaries = _split_long_segments(boundaries, clean_beats, max_len=max_len, target_len=target_len)
    return sorted({round(max(0.0, min(duration, b)), 3) for b in boundaries})


def _merge_short_segments(boundaries: list[float], *, min_len: float) -> list[float]:
    boundaries = list(boundaries)
    index = 1
    while len(boundaries) > 2 and index < len(boundaries):
        span = boundaries[index] - boundaries[index - 1]
        if span < min_len:
            if index == len(boundaries) - 1:
                boundaries.pop(index - 1)
            else:
                boundaries.pop(index)
            index = max(1, index - 1)
            continue
        index += 1
    return boundaries


def _split_long_segments(
    boundaries: list[float],
    beats: list[float],
    *,
    max_len: float,
    target_len: float,
) -> list[float]:
    output: list[float] = [boundaries[0]]
    for start, end in zip(boundaries, boundaries[1:]):
        cursor = start + target_len
        while end - output[-1] > max_len and cursor < end:
            boundary = snap_to_beats(cursor, beats)
            if output[-1] < boundary < end:
                output.append(round(boundary, 3))
            cursor += target_len
        output.append(end)
    return output


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _mean_between(energy: list[float], times: list[float], start: float, end: float) -> float:
    vals = [
        energy[i]
        for i in range(min(len(energy), len(times)))
        if start <= times[i] <= end
    ]
    return _mean(vals) if vals else _mean(energy)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _upper_quartile(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.75) - 1))
    return ordered[index]


def _first_between(values: list[float], start: float, end: float) -> float | None:
    for value in values:
        try:
            current = float(value)
        except (TypeError, ValueError):
            continue
        if start <= current <= end:
            return current
    return None


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
    """Annotate one BGM/audio asset into objective segments plus optional audio semantics."""
    extractor = feature_extractor or extract_audio_features
    try:
        features = dict(extractor(audio_path) or {})
    except Exception as exc:
        logger.warning("[bgm] feature extraction errored for %s: %s", asset_id, exc)
        features = {}

    raw_segments = features.get("segments") or []
    if not raw_segments:
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
    segments = _sensor_segments(raw_segments)
    if audio_profile is not None and audio_url_for_window is not None:
        enriched: list[BgmSegmentV4] = []
        for index, segment in enumerate(segments):
            updated, invocation_id = _listen_to_segment(
                gateway=gateway,
                profile=audio_profile,
                asset_id=asset_id,
                case_id=case_id,
                asset_title=asset_title,
                features=features,
                segment=segment,
                index=index,
                audio_url_for_window=audio_url_for_window,
            )
            if invocation_id:
                invocation_ids.append(invocation_id)
            enriched.append(updated)
        segments = enriched

    if any(segment.source == "sensor+audio" for segment in segments):
        status = "ok"
    elif audio_profile is None:
        status = LLM_UNCONFIGURED
    else:
        status = "sensor"
    annotation = _annotation_with_segments(
        asset_id=asset_id,
        case_id=case_id,
        duration=duration,
        features=features,
        segments=segments,
        status=status,
    )
    return BgmAnnotationResult(
        annotation=annotation,
        llm_configured=audio_profile is not None,
        provider_invocation_ids=invocation_ids,
    )


def _sensor_segments(raw_segments: list[Any]) -> list[BgmSegmentV4]:
    segments: list[BgmSegmentV4] = []
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            continue
        start = float(raw.get("start") or 0.0)
        end = float(raw.get("end") or 0.0)
        segments.append(
            BgmSegmentV4(
                segment_id=f"bgm_segment_{index + 1}",
                start=start,
                end=end,
                duration=float(raw.get("duration") or round(end - start, 3)),
                role=_role_from_hint(raw.get("role_hint")),
                drop_anchor_sec=raw.get("drop_anchor"),
                energy=float(raw.get("energy") or 0.0),
                source="sensor",
            )
        )
    return segments


def _listen_to_segment(
    *,
    gateway: ProviderGateway,
    profile: ProviderProfile,
    asset_id: str,
    case_id: str,
    asset_title: str,
    features: dict[str, Any],
    segment: BgmSegmentV4,
    index: int,
    audio_url_for_window: Callable[[float, float], str | None],
) -> tuple[BgmSegmentV4, str | None]:
    try:
        audio_uri = audio_url_for_window(segment.start, segment.end)
    except Exception as exc:
        logger.warning("[bgm] audio segment URL failed for %s/%s: %s", asset_id, index, exc)
        return segment, None
    if not audio_uri:
        return segment, None
    try:
        invocation, result = gateway.invoke(
            ProviderCall(
                case_id=case_id,
                provider_profile_id=profile.id,
                capability_id="audio.understanding",
                input={
                    "prompt": _build_segment_prompt(
                        asset_title=asset_title,
                        segment=segment,
                        features=features,
                    ),
                    "audio_uri": audio_uri,
                    "audio_seconds": segment.duration,
                    "asset_id": asset_id,
                    "segment_id": segment.segment_id,
                },
                idempotency_key=f"bgm-omni-{asset_id}-{index}",
            )
        )
    except Exception as exc:
        logger.warning("[bgm] audio semantic annotation failed for %s/%s: %s", asset_id, index, exc)
        return segment, None
    if result is None or invocation.error is not None:
        return segment, invocation.id
    intent = _intent_from_output(result.output)
    if not intent:
        return segment, invocation.id
    semantics = _normalize_segment_semantics(intent, role_hint=segment.role)
    return (
        segment.model_copy(
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


def _build_segment_prompt(
    *,
    asset_title: str,
    segment: BgmSegmentV4,
    features: dict[str, Any],
) -> str:
    payload = {
        "bgm_name": asset_title,
        "segment": {
            "start": segment.start,
            "end": segment.end,
            "duration": segment.duration,
            "energy": segment.energy,
            "has_drop": segment.drop_anchor_sec is not None,
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
        "你在听一段BGM音乐段落。结合你听到的音乐与给定信息，推断情绪/用途/适配场景，"
        "只返回一个合法 JSON 对象，不要 markdown 或多余文字。\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _intent_from_output(output: dict[str, Any]) -> dict[str, Any]:
    intent = output.get("intent") if isinstance(output, dict) else None
    if isinstance(intent, dict):
        return intent
    content = _content_from_output(output)
    return _extract_json_object(content) or {}


def _normalize_segment_semantics(
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
    segments: list[BgmSegmentV4] | None = None,
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
    if segments is not None:
        coverage_sec = round(sum(max(0.0, s.end - s.start) for s in segments), 3)
        track_duration = max((s.end for s in segments), default=0.0)
        bgm["segment_count"] = len(segments)
        bgm["annotated_coverage_sec"] = coverage_sec
        bgm["annotated_coverage_ratio"] = (
            round(coverage_sec / track_duration, 4) if track_duration > 0 else 0.0
        )
        bgm["recommended_segment_ids"] = [
            s.segment_id
            for s in segments
            if s.role in {BgmSegmentRole.hook, BgmSegmentRole.climax}
        ]
        bgm["source"] = (
            "sensor+audio" if any(s.source == "sensor+audio" for s in segments) else "sensor"
        )
        semantic_segment = next((s for s in segments if s.source == "sensor+audio"), None)
        if semantic_segment is not None:
            bgm.update(
                {
                    "mood": semantic_segment.mood,
                    "scene_fit": semantic_segment.scene_fit,
                    "avoid_scene": semantic_segment.avoid_scene,
                    "retrieval_text": " ".join(
                        part
                        for part in (
                            semantic_segment.mood,
                            semantic_segment.reason,
                            *semantic_segment.scene_fit,
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


def _annotation_with_segments(
    *,
    asset_id: str,
    case_id: str,
    duration: float,
    features: dict[str, Any],
    segments: list[BgmSegmentV4],
    status: str,
) -> AnnotationV4:
    return AnnotationV4(
        meta=_meta(asset_id, case_id, duration, AnnotationStatus.completed),
        bgm_segments=segments,
        quality_report=_bgm_quality_report(
            features=features,
            segments=segments,
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
