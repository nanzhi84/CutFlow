"""Portrait-candidate scoring helpers (recency / source-mode / option score).

Ported from editing_agent/candidate_scorer.py (the portrait subset) +
boundary_planning's candidate scoring. A "candidate" is a portrait source-window
dict carrying: window_id, template_id, duration, role, confidence, source_mode_hint,
recency_penalty / recent_usage (recency context), diversity_key, source, etc. These
are pure scoring functions over those dicts.
"""

from __future__ import annotations

from typing import Any

from packages.planning.editing import _util as util


def candidate_source_mode(candidate: dict[str, Any]) -> str:
    raw = str(candidate.get("source_mode_hint") or "lipsynced").strip().lower()
    if raw in {"original", "voiceover", "raw", "non_lipsynced", "no_lipsync"}:
        return "original"
    return "lipsynced"


def candidate_recency_penalty(candidate: dict[str, Any]) -> float:
    values = [
        candidate.get("recency_penalty"),
        (candidate.get("score_breakdown") or {}).get("recency_penalty")
        if isinstance(candidate.get("score_breakdown"), dict)
        else None,
    ]
    v2_payload = candidate.get("v2") if isinstance(candidate.get("v2"), dict) else {}
    if isinstance(v2_payload.get("score_breakdown"), dict):
        values.append(v2_payload["score_breakdown"].get("recency_penalty"))
    if isinstance(v2_payload.get("recency"), dict):
        values.append(v2_payload["recency"].get("recency_penalty"))

    best = 0.0
    for value in values:
        try:
            best = max(best, float(value or 0.0))
        except Exception:
            continue
    return round(best, 3)


def is_recent_portrait_candidate(candidate: dict[str, Any]) -> bool:
    recent_usage = candidate.get("recent_usage") if isinstance(candidate.get("recent_usage"), dict) else {}
    if recent_usage:
        return bool(recent_usage.get("is_recently_used")) or int(
            recent_usage.get("recent_task_use_count") or 0
        ) > 0
    return bool(candidate.get("recently_used")) or "近期已用" in [
        str(label).strip() for label in (candidate.get("labels") or [])
    ]


def portrait_recent_usage_penalty(candidate: dict[str, Any], *, fallback_multiplier: float = 1.0) -> float:
    recent_usage = candidate.get("recent_usage") if isinstance(candidate.get("recent_usage"), dict) else {}
    direct_penalty = candidate_recency_penalty(candidate)
    if not recent_usage and direct_penalty <= 0:
        return 0.0
    task_count = int(recent_usage.get("recent_task_use_count") or 0)
    segment_count = int(recent_usage.get("recent_segment_use_count") or 0)
    opening_count = int(recent_usage.get("recent_opening_use_count") or 0)
    is_exact_recent = bool(recent_usage.get("is_recently_used")) or task_count > 0
    if not is_exact_recent:
        similarity_penalty = max(direct_penalty, util.as_float(recent_usage.get("similarity_penalty"), 0.0))
        if similarity_penalty <= 0:
            return 0.0
        return round(min(8.0, similarity_penalty * 12.0) * max(0.0, fallback_multiplier), 3)
    if not (is_exact_recent or direct_penalty > 0):
        return 0.0
    usage_penalty = 22.0 + task_count * 8.0 + segment_count * 1.2 + opening_count * 10.0
    normalized_penalty = 14.0 + direct_penalty * 34.0 if direct_penalty > 0 else 0.0
    penalty = min(48.0, max(usage_penalty, normalized_penalty))
    return round(penalty * max(0.0, fallback_multiplier), 3)


def portrait_boundary_option_score(
    *,
    candidate: dict[str, Any],
    chunk: dict[str, Any],
    template_usage_count: int,
    ignore_repetition_penalty: bool = False,
) -> float:
    duration = util.round_time(chunk.get("duration", 0.0))
    available_duration = util.round_time(candidate.get("duration", 0.0))
    role = str(candidate.get("role") or "main").lower()
    phase = str(chunk.get("phase") or "main").lower()
    if phase == "opening":
        role_bonus = {"hook": 8.0, "main": 4.0, "backup": 1.0}.get(role, 0.0)
    elif phase == "tail":
        role_bonus = {"main": 4.0, "backup": 3.0, "hook": 0.5}.get(role, 0.0)
    else:
        role_bonus = {"main": 6.0, "backup": 2.5, "hook": 1.0}.get(role, 0.0)

    score = 0.0
    score += role_bonus
    score += util.as_float(candidate.get("confidence"), 0.0) * 3.0
    score -= abs(available_duration - duration) * 0.45
    if not ignore_repetition_penalty:
        score -= template_usage_count * 4.0
    if str(candidate.get("source") or "") == "quality_valid_segments":
        score -= 1.5
    score -= portrait_recent_usage_penalty(
        candidate, fallback_multiplier=0.5 if ignore_repetition_penalty else 1.0
    )
    return score


def make_boundary_beam_score_fn(*, relax: dict[str, Any], ignore_repetition_penalty: bool):
    """Per-candidate scoring + hard constraints (returns base score or None)."""
    max_uses = int(relax.get("max_uses", 999))
    allow_adjacent = bool(relax.get("allow_adjacent", True))
    allow_original = bool(relax.get("allow_original", True))

    def score_fn(chunk: dict[str, Any], candidate: dict[str, Any], state: dict[str, Any]) -> float | None:
        window_id = str(candidate.get("window_id") or "").strip()
        template_id = str(candidate.get("template_id") or "").strip()
        if not window_id or not template_id:
            return None
        duration = util.round_time(chunk.get("duration", 0.0))
        used_duration = util.round_time(state["window_used"].get(window_id, 0.0))
        available_duration = util.round_time(candidate.get("duration", 0.0) - used_duration)
        if available_duration + 1e-3 < duration:
            return None
        if state["template_counts"].get(template_id, 0) >= max_uses:
            return None
        if not allow_adjacent and state["last_template"] == template_id:
            return None
        if candidate_source_mode(candidate) == "original" and not allow_original:
            return None
        diversity_key = str(candidate.get("diversity_key") or "").strip()
        score = portrait_boundary_option_score(
            candidate=candidate,
            chunk=chunk,
            template_usage_count=state["template_counts"].get(template_id, 0),
            ignore_repetition_penalty=ignore_repetition_penalty,
        )
        if diversity_key and not ignore_repetition_penalty:
            score -= state["diversity_counts"].get(diversity_key, 0) * 5.0
        if used_duration > 0:
            score -= used_duration * (0.2 if ignore_repetition_penalty else 0.75)
        return score

    return score_fn
