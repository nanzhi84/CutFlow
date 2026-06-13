"""V4 unified annotation pipeline ``run_annotation_v4`` - the V4 protocol core.

Ported from the b-roll analyzer's ``annotation_v4_pipeline`` (orchestration only;
the sensor suite it builds on lives in this package's ``sensors`` / ``boundary`` /
``windows`` / ``report`` modules - it is NOT re-ported here).

Two-layer "sensors + brain" design:

- deterministic tools (PySceneDetect / Silero VAD / CV) emit objective signals,
  the VLM only judges semantics. This pipeline ONLY orchestrates; every real-world
  dependency (sensors / frame extraction / VLM / ASR / prompt) is injected via
  :class:`V4Deps`, so it is fully mockable and never touches real
  ffmpeg / VLM / scenedetect / silero in tests.
- retry, never degrade: per-window VLM with failure-typed retries -
    * :class:`SchemaError`   -> change the prompt, keep frames; cap ``fmt_max_retries``.
    * :class:`SemanticError` -> resample (denser frames) + adjust phrasing; cap ``sem_max_retries``.
    * :class:`RuntimeVLMError`-> pure exponential backoff, same frames/prompt; cap ``rt_max_retries``.
    * :class:`UnrecoverableError` -> no retry, fail directly.
  Any window exhausting its retries -> :class:`WindowFailed` -> the WHOLE asset gets
  ``annotation_status=failed`` with empty clips/quality_report/usage_windows (it NEVER
  writes a degraded annotation; V4 has no needs_review). Failure is expressed by
  RETURNING a FAILED object, not by raising to the caller (one bad asset can't sink a batch).

Synchronous and dependency-light: this version runs windows serially (the gated
runner bridges the async gateway). Frames are sampled deterministically; the
cross-window text channel feeds the whole-clip ASR text to every window prompt.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from packages.core.contracts import (
    AnnotationMetaV4,
    AnnotationStatus,
    AnnotationV4,
    AnnotationVersion,
    ClipV4,
)

from . import _assemble as assemble
from . import vlm as vlm_module
from . import windows as window_planner
from .errors import (
    AnnotationV4Error,
    RuntimeVLMError,
    SchemaError,
    SemanticError,
    UnrecoverableError,
)
from .report import build_quality_report

logger = logging.getLogger("packages.media.annotation.pipeline")


# ===========================================================================
# Failure signal: a window exhausted its retries.
# ===========================================================================
class WindowFailed(AnnotationV4Error):
    """A window exhausted its retries -> trigger an explicit whole-asset failure.

    Carries the window interval + reason so the caller can log why (for re-supply
    / re-upload). Never carries a degraded annotation.
    """

    def __init__(self, window_start: float, window_end: float, reason: str) -> None:
        self.window_start = window_start
        self.window_end = window_end
        self.reason = reason
        super().__init__(f"window [{window_start:.3f}, {window_end:.3f}] failed annotation: {reason}")


def _default_build_prompt(**kwargs: Any) -> str:
    return vlm_module.build_window_prompt(**kwargs)


def _default_parse_response(raw: str, **kwargs: Any) -> list[ClipV4]:
    return vlm_module.parse_window_response(raw, **kwargs)


def _noop_sleep(_seconds: float) -> None:
    return None


@dataclass
class V4Deps:
    """All external dependencies of the V4 pipeline (sensors / frames / VLM / ASR).

    Every member is an injected callable, keeping the pipeline pure orchestration
    and fully mockable - no real ffmpeg / VLM / scenedetect / silero in unit tests.
    """

    detect_shot_cuts: Callable[[str], list[float]]
    detect_speech_islands: Callable[[str], list]
    detect_quality_events: Callable[[str], list[dict]]
    extract_frames: Callable[..., list[tuple[float, str]]]
    vlm_call: Callable[[str, list], str]
    resolve_asr_text: Callable[[str], str]
    sleep: Callable[[float], None] = _noop_sleep
    build_prompt: Callable[..., str] = _default_build_prompt
    parse_response: Callable[..., list[ClipV4]] = _default_parse_response


@dataclass
class V4Config:
    """V4 pipeline parameters (defaults; the runner overrides as needed)."""

    window_min_sec: float = 3.0
    window_max_sec: float = 10.0
    window_frame_budget: int = 14
    frame_max_long_side: int = 1024
    vad_adhesion_range: float = 0.5
    vad_merge_eps: float = 0.1
    edge_window: float = 2.0
    fps_assumed: float = 25.0
    inset_frames: int = 1
    snap_tol: float = 0.25
    internal_cut_edge_guard: float = 0.12
    fmt_max_retries: int = 2
    sem_max_retries: int = 3
    rt_max_retries: int = 5
    min_confidence: float = 0.3


# ===========================================================================
# Entry
# ===========================================================================
def run_annotation_v4(
    *,
    asset_id: str,
    case_id: str,
    material_type: str,
    video_path: str,
    duration: float,
    deps: V4Deps,
    cfg: V4Config = V4Config(),
) -> AnnotationV4:
    """V4 unified annotation pipeline (portrait + b-roll share one path).

    Flow:
      (1) sensors: shot cuts / speech islands (portrait only) / CV quality events;
      (2) window planning + whole-clip ASR text (cross-window text channel);
      (3) per-window frame extraction + VLM (classified retries);
      (4) any window failure -> whole asset FAILED (returns object, does not raise);
      (5) all windows OK -> boundary refine + aggregate quality_report / usage_windows -> COMPLETED.

    Returns:
        AnnotationV4. On failure ``meta.annotation_status=failed`` with empty
        clips/quality_report/usage_windows (never a degraded annotation), still a
        valid V4 schema.
    """
    is_portrait = _is_portrait(material_type)

    # -- (1) sensor layer (deterministic) --
    try:
        shot_cuts = list((deps.detect_shot_cuts(video_path)) or [])
    except Exception as exc:  # fail-open: treat as no cuts
        logger.warning("[V4] shot-cut detection error, treating as no cuts: %s", exc)
        shot_cuts = []

    speech_islands: list[dict[str, float]] = []
    if is_portrait:
        try:
            raw_islands = (deps.detect_speech_islands(video_path)) or []
            speech_islands = [_island_to_dict(i) for i in raw_islands]
        except Exception as exc:
            logger.warning("[V4] speech detection error, treating as no speech: %s", exc)
            speech_islands = []

    try:
        cv_events = list((deps.detect_quality_events(video_path)) or [])
    except Exception as exc:
        logger.warning("[V4] CV quality detection error, treating as no events: %s", exc)
        cv_events = []

    # -- (2) window planning + whole-clip ASR text --
    windows = window_planner.plan_windows(
        duration=duration,
        shot_cuts=shot_cuts,
        speech_islands=speech_islands if is_portrait else None,
        window_min_sec=cfg.window_min_sec,
        window_max_sec=cfg.window_max_sec,
        vad_adhesion_range=cfg.vad_adhesion_range,
        vad_merge_eps=cfg.vad_merge_eps,
    )

    try:
        full_asr_text = (deps.resolve_asr_text(video_path)) or ""
    except Exception as exc:
        logger.warning("[V4] ASR text fetch error, treating as empty: %s", exc)
        full_asr_text = ""

    sensor_signals = _build_sensor_signals(shot_cuts, speech_islands, cv_events)

    # -- (3) per-window frame extraction + VLM (classified retries) --
    all_clips: list[ClipV4] = []
    try:
        for win in windows:
            window_clips = _analyze_window_with_retry(
                window_start=win.start,
                window_end=win.end,
                material_type=material_type,
                video_path=video_path,
                duration=duration,
                sensor_signals=sensor_signals,
                full_asr_text=full_asr_text,
                deps=deps,
                cfg=cfg,
            )
            all_clips.extend(window_clips)
    except WindowFailed as wf:
        # -- (4) any window failure -> whole asset FAILED (return object, no raise) --
        logger.warning("[V4] asset %s annotation failed (window failed): %s", asset_id, wf)
        return _failed_annotation(
            asset_id=asset_id,
            case_id=case_id,
            material_type=material_type,
            duration=duration,
        )

    # -- (5) all windows OK -> boundary refine + aggregate --
    refined_clips = assemble.refine_clip_boundaries(
        all_clips,
        shot_cuts,
        duration,
        material_type=material_type,
        fps_assumed=cfg.fps_assumed,
        inset_frames=cfg.inset_frames,
        snap_tol=cfg.snap_tol,
        internal_cut_edge_guard=cfg.internal_cut_edge_guard,
    )
    quality_events = assemble.assemble_quality_events(cv_events, duration)
    quality_report = build_quality_report(
        material_type=material_type,
        duration=duration,
        clips=refined_clips,
        quality_events=quality_events,
    )
    usage_windows = assemble.build_usage_windows(refined_clips)
    evidence_frames = assemble.collect_evidence_frames(refined_clips, duration)

    meta = AnnotationMetaV4(
        annotation_version=AnnotationVersion.v4,
        asset_id=asset_id,
        case_id=case_id,
        material_type=material_type,
        duration=max(0.0, float(duration or 0.0)),
        annotation_status=AnnotationStatus.completed,
    )
    return AnnotationV4(
        meta=meta,
        clips=refined_clips,
        quality_events=quality_events,
        quality_report=quality_report,
        usage_windows=usage_windows,
        evidence_frames=evidence_frames,
    )


# ===========================================================================
# Per-window analysis + classified retries
# ===========================================================================
def _analyze_window_with_retry(
    *,
    window_start: float,
    window_end: float,
    material_type: str,
    video_path: str,
    duration: float,
    sensor_signals: dict[str, Any],
    full_asr_text: str,
    deps: V4Deps,
    cfg: V4Config,
) -> list[ClipV4]:
    """Retry one window, routing by failure type; exhausting any cap raises WindowFailed.

    Retry counts accumulate per type (independent caps). Frames are (re)sampled on
    first use and on SemanticError; SchemaError changes the prompt only;
    RuntimeVLMError backs off with the same frames/prompt.
    """
    fmt_attempt = 0
    sem_attempt = 0
    rt_attempt = 0
    retry_hint = ""
    density_level = 0
    temp_dirs: list[str] = []

    def _extract(level: int) -> list[tuple[float, str]]:
        frames, temp_dir = _sample_and_extract(
            window_start=window_start,
            window_end=window_end,
            material_type=material_type,
            video_path=video_path,
            sensor_signals=sensor_signals,
            density_level=level,
            deps=deps,
            cfg=cfg,
        )
        temp_dirs.append(temp_dir)
        return frames

    frames = _extract(density_level)
    try:
        while True:
            prompt = deps.build_prompt(
                material_type=material_type,
                window_start=window_start,
                window_end=window_end,
                sensor_signals=sensor_signals,
                full_asr_text=full_asr_text,
                retry_hint=retry_hint,
            )
            try:
                raw = deps.vlm_call(prompt, frames)
                clips = list(
                    deps.parse_response(
                        raw,
                        material_type=material_type,
                        window_start=window_start,
                        window_end=window_end,
                        duration=duration,
                        min_confidence=cfg.min_confidence,
                    )
                )
                return clips
            except SchemaError as exc:
                if fmt_attempt >= cfg.fmt_max_retries:
                    raise WindowFailed(window_start, window_end, f"schema retries exhausted: {exc}")
                fmt_attempt += 1
                retry_hint = _schema_retry_hint()
            except SemanticError as exc:
                if sem_attempt >= cfg.sem_max_retries:
                    raise WindowFailed(window_start, window_end, f"semantic retries exhausted: {exc}")
                sem_attempt += 1
                density_level += 1
                retry_hint = _semantic_retry_hint()
                frames = _extract(density_level)
            except RuntimeVLMError as exc:
                if rt_attempt >= cfg.rt_max_retries:
                    raise WindowFailed(window_start, window_end, f"runtime retries exhausted: {exc}")
                deps.sleep(float(2**rt_attempt))
                rt_attempt += 1
            except UnrecoverableError as exc:
                raise WindowFailed(window_start, window_end, f"unrecoverable: {exc}")
    finally:
        for temp_dir in temp_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ===========================================================================
# Frame sampling: window-internal hot-spot sampling + downscale
# ===========================================================================
def _sample_and_extract(
    *,
    window_start: float,
    window_end: float,
    material_type: str,
    video_path: str,
    sensor_signals: dict[str, Any],
    density_level: int,
    deps: V4Deps,
    cfg: V4Config,
) -> tuple[list[tuple[float, str]], str]:
    """Pick sample times inside the window and extract (downscaled) frames.

    ``density_level`` (incremented by SemanticError) makes resamples denser so the
    VLM gets a fresh viewpoint. Returns ``(frames, temp_dir)``; the caller cleans up.
    """
    budget = max(1, int(cfg.window_frame_budget)) + max(0, density_level) * 4
    sample_times = assemble.pick_window_sample_times(
        window_start=window_start,
        window_end=window_end,
        material_type=material_type,
        sensor_signals=sensor_signals,
        budget=budget,
        edge_window=cfg.edge_window,
    )
    temp_dir = f"/tmp/v4_frames_{uuid.uuid4().hex[:8]}"
    frames = list(
        deps.extract_frames(
            video_path,
            sample_times,
            temp_dir=temp_dir,
            max_long_side=cfg.frame_max_long_side,
        )
    )
    return frames, temp_dir


# ===========================================================================
# Failed annotation (never a degraded annotation)
# ===========================================================================
def _failed_annotation(
    *,
    asset_id: str,
    case_id: str,
    material_type: str,
    duration: float,
) -> AnnotationV4:
    """Build a FAILED annotation: clips / quality_events / quality_report / usage_windows all empty."""
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
        quality_events=[],
        quality_report={},
        usage_windows=[],
        evidence_frames=[],
    )


# ===========================================================================
# Helpers
# ===========================================================================
def _build_sensor_signals(
    shot_cuts: list[float],
    speech_islands: list[dict[str, float]],
    cv_events: list[dict],
) -> dict[str, Any]:
    """Pack the sensor-signal summary fed to the prompt / sampler."""
    return {
        "shot_cuts": [round(float(c), 3) for c in (shot_cuts or [])],
        "speech_islands": list(speech_islands or []),
        "quality_events": [
            {
                "event_type": _event_field(ev, "event_type"),
                "start": _event_field(ev, "start"),
                "end": _event_field(ev, "end"),
                "source": _event_field(ev, "source") or "sensor",
            }
            for ev in (cv_events or [])
        ],
    }


def _event_field(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def _island_to_dict(island: Any) -> dict[str, float]:
    """Normalize a SpeechIsland (model / dict) into {start, end, confidence}."""
    if isinstance(island, dict):
        start = _safe_float(island.get("start")) or 0.0
        end = _safe_float(island.get("end")) or 0.0
        conf = _safe_float(island.get("confidence"))
    else:
        start = _safe_float(getattr(island, "start", None)) or 0.0
        end = _safe_float(getattr(island, "end", None)) or 0.0
        conf = _safe_float(getattr(island, "confidence", None))
    out: dict[str, float] = {"start": start, "end": end}
    if conf is not None:
        out["confidence"] = conf
    return out


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_portrait(material_type: str) -> bool:
    mt = str(material_type or "").strip().lower()
    return any(tok in mt for tok in ("portrait", "口播", "talk"))


def _schema_retry_hint() -> str:
    return (
        "上一次输出格式不合法。必须严格输出**单个 JSON 对象**,顶层含 \"segments\" 数组;"
        "禁止任何 Markdown 代码块包装(不要 ```json);禁止额外解释文字;"
        "每个 segment 的 start/end 必须 start<end,role 必须取 hook/main/backup/avoid/cover 之一。"
    )


def _semantic_retry_hint() -> str:
    return (
        "上一次内容不合理。请确保 segments **连续无缝覆盖整个窗口**(无大空隙、不越界);"
        "role 与 recommended_for_lip_sync 不得自相矛盾(如 role=main 必须可对口型);"
        "confidence 应反映真实把握度,不要给过低分;已附更密的采样帧,请据新帧重判。"
    )
