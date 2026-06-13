"""V4 whole-clip health report - deterministic aggregator (no VLM).

Input: V4 clips / quality_events. Pure functions compute whole-clip metrics, one
set for portrait (talking-head) and one for b-roll (scenery):

- portrait -> {hook_strength, speech_stability, tail_state, lip_sync_suitability_score}
- broll    -> {usable_ratio, stability_score, hard_quality_count, soft_quality_count,
               dominant_scene_types, dominant_shot_scales}

Input accepts pydantic ClipV4/QualityEventV4 instances or equivalent dicts
(normalized via model_dump), so the assembler and tests can share it.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Any

from ._util import is_explicit_false, overlap_duration, to_float

# Event types affecting b-roll stability (motion_guard + blur/freeze).
_STABILITY_EVENT_TYPES = {"shake", "blur", "camera_drop"}
# soft_quality_count counts only these (independent of risk_tier).
_SOFT_EVENT_TYPES = {"shake", "blur"}
# dominant_* ratio threshold.
_DOMINANT_MIN_RATIO = 0.15
# Portrait tail scan window (sec).
_TAIL_WINDOW = 2.0
# Portrait hook scan window (sec).
_HOOK_WINDOW = 3.0


def _enum_str(value: Any) -> str:
    """Normalize an Enum / str into a lowercase bare string.

    pydantic model_dump() keeps Enum instances; str() on them gives the wrong
    'classname.member', so take .value.
    """
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").strip().lower()


def _as_dict(item: Any) -> dict[str, Any]:
    """Normalize a pydantic model / dict to a dict (other types -> {})."""
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            return {}
    return {}


def merged_event_duration(intervals: Sequence[tuple[float, float]]) -> float:
    """Total duration of the union of [start, end) intervals.

    Overlaps count once; touching intervals merge; containment takes the outer;
    zero-length / reversed intervals are ignored.
    """
    spans: list[tuple[float, float]] = []
    for start, end in intervals:
        s = to_float(start, 0.0)
        e = to_float(end, 0.0)
        if e > s:
            spans.append((s, e))
    if not spans:
        return 0.0

    spans.sort(key=lambda pair: pair[0])
    total = 0.0
    cur_start, cur_end = spans[0]
    for s, e in spans[1:]:
        if s <= cur_end:
            cur_end = max(cur_end, e)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = s, e
    total += cur_end - cur_start
    return total


# ===========================================================================
# Portrait metrics
# ===========================================================================
def _clip_blockers(clip: dict[str, Any], hard_event: bool) -> list[str]:
    """Derive blocker signals from a V4 clip. ``hard_event`` = a hard quality
    event overlaps this clip's time."""
    usage = _as_dict(clip.get("usage"))
    semantics = _as_dict(clip.get("semantics"))
    blockers: list[str] = []

    if not bool(usage.get("recommended_for_lip_sync", False)):
        blockers.append("not_recommended_lip_sync")
    if _enum_str(usage.get("role")) == "avoid":
        blockers.append("marked_avoid")
    if hard_event:
        blockers.append("hard_risk")
    if is_explicit_false(semantics.get("gaze_to_camera")):
        blockers.append("gaze_off")
    if is_explicit_false(semantics.get("mouth_visible")):
        blockers.append("mouth_not_visible")
    if is_explicit_false(semantics.get("mouth_moving")):
        blockers.append("mouth_not_moving")
    try:
        if int(semantics.get("face_count_max") or 0) > 1:
            blockers.append("multi_face")
    except (TypeError, ValueError):
        pass
    if str(semantics.get("speech_action_alignment") or "").strip().lower() == "mismatch":
        blockers.append("speech_action_mismatch")
    retake_cue = str(semantics.get("retake_cue") or "").strip().lower()
    if retake_cue and retake_cue != "none":
        blockers.append("retake_signal")
    return list(dict.fromkeys(blockers))


def _has_hard_overlap(
    start: float, end: float, hard_event_spans: Sequence[tuple[float, float]]
) -> bool:
    """Whether this clip overlaps any hard-risk event interval."""
    for ev_start, ev_end in hard_event_spans:
        if overlap_duration(start, end, ev_start, ev_end) > 0:
            return True
    return False


def _build_portrait_report(
    duration: float,
    clips: list[dict[str, Any]],
    quality_events: list[dict[str, Any]],
) -> dict[str, Any]:
    total_duration = max(0.0, duration)

    hard_event_spans: list[tuple[float, float]] = []
    for ev in quality_events:
        if _enum_str(ev.get("risk_tier")) == "hard":
            s = to_float(ev.get("start"), 0.0)
            e = to_float(ev.get("end"), s)
            if e > s:
                hard_event_spans.append((s, e))

    positive_duration = 0.0
    first3_positive = 0.0
    has_hook_signal = False
    blocker_count = 0
    tail_tokens: list[str] = []
    tail_window_start = max(
        0.0, total_duration - min(_TAIL_WINDOW, max(total_duration, 0.0))
    )

    for clip in clips:
        start = to_float(clip.get("start"), 0.0)
        end = to_float(clip.get("end"), start)
        if end <= start:
            continue
        usage = _as_dict(clip.get("usage"))
        semantics = _as_dict(clip.get("semantics"))
        retrieval = _as_dict(clip.get("retrieval"))

        hard_event = _has_hard_overlap(start, end, hard_event_spans)
        blockers = _clip_blockers(clip, hard_event)

        if blockers:
            blocker_count += 1
        else:
            seg_duration = max(0.0, end - start)
            positive_duration += seg_duration
            first3_positive += overlap_duration(start, end, 0.0, _HOOK_WINDOW)
            role = _enum_str(usage.get("role"))
            intent = _enum_str(semantics.get("speaker_intent"))
            if role == "hook" or "hook" in intent or "钩子" in intent:
                has_hook_signal = True

        if (
            total_duration <= 0
            or overlap_duration(start, end, tail_window_start, total_duration) > 0
        ):
            tail_tokens.extend(blockers)
            tail_tokens.append(str(semantics.get("retake_cue") or ""))
            tail_tokens.append(str(retrieval.get("summary") or ""))

    if total_duration > 0:
        for ev in quality_events:
            s = to_float(ev.get("start"), 0.0)
            e = to_float(ev.get("end"), s)
            if e <= s:
                continue
            if overlap_duration(s, e, tail_window_start, total_duration) > 0:
                tail_tokens.append(_enum_str(ev.get("event_type")))
                tail_tokens.append(str(ev.get("description") or ""))

    positive_ratio = (positive_duration / total_duration) if total_duration > 0 else 0.0

    hook_strength = (
        "strong"
        if has_hook_signal or first3_positive >= 1.2
        else ("medium" if first3_positive >= 0.5 else "weak")
    )
    speech_stability = (
        "stable"
        if blocker_count == 0 and positive_ratio >= 0.75
        else ("fair" if positive_ratio >= 0.45 else "unstable")
    )

    tail_text = " ".join(token for token in tail_tokens if token).lower()
    tail_state = "clean"
    if any(
        token in tail_text
        for token in ("笑", "ng", "重来", "retake", "blooper", "retake_signal")
    ):
        tail_state = "blooper"
    elif any(
        token in tail_text
        for token in (
            "离镜",
            "出画",
            "mouth_not_visible",
            "gaze_off",
            "exit_frame",
            "look_off",
        )
    ):
        tail_state = "off_screen"
    elif any(token in tail_text for token in ("抖", "shake", "晃", "blur")):
        tail_state = "shaky"

    lip_sync_score = int(
        round(max(0.0, min(100.0, 55.0 + positive_ratio * 40.0 - blocker_count * 6.0)))
    )
    return {
        "hook_strength": hook_strength,
        "speech_stability": speech_stability,
        "tail_state": tail_state,
        "lip_sync_suitability_score": lip_sync_score,
    }


# ===========================================================================
# B-roll (scenery) metrics
# ===========================================================================
def _dominant_types(values: list[str]) -> list[str]:
    """Frequency count; return types with ratio >= 15% (frequency desc, ties by first seen)."""
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    total = len(cleaned)
    if total == 0:
        return []
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for idx, value in enumerate(cleaned):
        if value not in counts:
            counts[value] = 0
            order[value] = idx
        counts[value] += 1
    qualified = [v for v in counts if counts[v] / total >= _DOMINANT_MIN_RATIO]
    qualified.sort(key=lambda v: (-counts[v], order[v]))
    return qualified


def _build_broll_report(
    duration: float,
    clips: list[dict[str, Any]],
    quality_events: list[dict[str, Any]],
) -> dict[str, Any]:
    total_duration = max(0.0, duration)

    usable_spans: list[tuple[float, float]] = []
    scene_types: list[str] = []
    shot_scales: list[str] = []
    for clip in clips:
        start = to_float(clip.get("start"), 0.0)
        end = to_float(clip.get("end"), start)
        if end <= start:
            continue
        usage = _as_dict(clip.get("usage"))
        semantics = _as_dict(clip.get("semantics"))
        visual = _as_dict(clip.get("visual"))
        if _enum_str(usage.get("role")) != "avoid":
            usable_spans.append((start, end))
        scene_types.append(str(semantics.get("scene_type") or ""))
        shot_scales.append(str(visual.get("shot_scale") or ""))

    usable_duration = merged_event_duration(usable_spans)
    usable_ratio = (usable_duration / total_duration) if total_duration > 0 else 0.0
    usable_ratio = max(0.0, min(1.0, usable_ratio))

    stability_spans: list[tuple[float, float]] = []
    hard_quality_count = 0
    soft_quality_count = 0
    for ev in quality_events:
        event_type = _enum_str(ev.get("event_type"))
        risk_tier = _enum_str(ev.get("risk_tier"))
        if risk_tier == "hard":
            hard_quality_count += 1
        if event_type in _SOFT_EVENT_TYPES:
            soft_quality_count += 1
        if event_type in _STABILITY_EVENT_TYPES:
            s = to_float(ev.get("start"), 0.0)
            e = to_float(ev.get("end"), s)
            if e > s:
                stability_spans.append((s, e))

    if total_duration > 0:
        unstable_duration = merged_event_duration(stability_spans)
        stability_score = (total_duration - unstable_duration) / total_duration * 100.0
        stability_score = max(0.0, min(100.0, stability_score))
    else:
        stability_score = 100.0

    return {
        "usable_ratio": usable_ratio,
        "stability_score": stability_score,
        "hard_quality_count": hard_quality_count,
        "soft_quality_count": soft_quality_count,
        "dominant_scene_types": _dominant_types(scene_types),
        "dominant_shot_scales": _dominant_types(shot_scales),
    }


# ===========================================================================
# Public interface
# ===========================================================================
def _is_portrait(material_type: str) -> bool:
    mt = str(material_type or "").strip().lower()
    return (
        mt in {"portrait", "口播", "talking", "talking_head"}
        or "portrait" in mt
        or "口播" in mt
    )


def _is_broll(material_type: str) -> bool:
    mt = str(material_type or "").strip().lower()
    return mt in {"scenery", "broll", "b_roll", "b-roll", "空镜", "产品"} or any(
        token in mt for token in ("scenery", "broll", "b_roll", "b-roll", "空镜")
    )


def build_quality_report(
    *,
    material_type: str,
    duration: float,
    clips: list,
    quality_events: list,
) -> dict:
    """Deterministic whole-clip health aggregation (no VLM).

    portrait -> {hook_strength, speech_stability, tail_state, lip_sync_suitability_score}
    broll    -> {usable_ratio, stability_score, hard_quality_count, soft_quality_count,
                 dominant_scene_types, dominant_shot_scales}
    unknown material_type -> {} (no guessing).

    manual_note events are free-form human notes, excluded from deterministic risk
    aggregation regardless of their risk_tier.
    """
    duration_f = to_float(duration, 0.0)
    clip_dicts = [_as_dict(c) for c in (clips or [])]
    event_dicts = [
        d
        for d in (_as_dict(e) for e in (quality_events or []))
        if _enum_str(d.get("event_type")) != "manual_note"
    ]

    if _is_portrait(material_type):
        return _build_portrait_report(duration_f, clip_dicts, event_dicts)
    if _is_broll(material_type):
        return _build_broll_report(duration_f, clip_dicts, event_dicts)
    return {}
