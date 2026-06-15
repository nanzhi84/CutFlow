"""Case self-evolution closed loop — pure logic (Spec §8.4 / §25.4-25.8).

Storage-agnostic helpers shared by the in-memory service path and the
SQLAlchemy repository path:

- ``compute_performance_score``  (§25.6): normalized, windowed, confidence-gated.
- ``extract_script_features`` / ``extract_video_features`` (§25.5): partial then
  complete CreativeFeatureVector derivation.
- ``filter_recall_memories``     (§25.8): scope/validity-window/confidence recall
  with topic/platform/memory_type/recent/high-/low-performance modes.
- ``analyze_historical_performance`` (HistoricalPerformanceAnalysisNode §8.4):
  group observations + scores by platform/account/window with sample_size.
- ``build_memory_proposals``     (§8.4 CaseReflectionNode): derive data-driven
  proposals from analysis + briefs/candidates, with dedup against existing
  active + proposed memories.

Everything here operates on ``packages.core.contracts`` models so it is trivially
unit-testable without a database.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Iterable, Sequence

from packages.core.contracts import (
    CaseMemory,
    CaseMemoryScope,
    CreativeBrief,
    CreativeFeatureVector,
    MemoryProposal,
    MemoryRecallMode,
    PerformanceObservation,
    PerformanceScore,
    ScriptVersion,
    VideoVersion,
    utcnow,
)

# §25.6: below this impression/view volume we never write a high-confidence
# conclusion (impressions/views must not be treated as quality directly).
MIN_CONFIDENT_IMPRESSIONS = 1000

# §25.6: 24h is only an early signal; 7d/30d windows are eligible for active memory.
EARLY_SIGNAL_WINDOWS = frozenset({"1h", "24h"})
ACTIVE_ELIGIBLE_WINDOWS = frozenset({"7d", "30d"})

# §25.7: an active memory must clear both thresholds (or be force-activated).
MEMORY_ACTIVATION_MIN_CONFIDENCE = 0.6
MEMORY_ACTIVATION_MIN_SAMPLE_SIZE = 3


# ---------------------------------------------------------------------------
# §25.6 PerformanceScore
# ---------------------------------------------------------------------------

def _engagement_rate(obs: PerformanceObservation) -> float | None:
    parts = [obs.like_rate, obs.comment_rate, obs.share_rate, obs.follow_rate]
    present = [p for p in parts if p is not None]
    if not present:
        return None
    return sum(present)


def _primary_metric_value(
    obs: PerformanceObservation,
) -> tuple[str, float | None]:
    """Pick the most meaningful normalized signal available on the observation."""
    if obs.conversion_rate is not None:
        return "conversion_rate", obs.conversion_rate
    if obs.follow_rate is not None:
        return "follow_rate", obs.follow_rate
    if obs.completion_rate is not None:
        return "completion_rate", obs.completion_rate
    engagement = _engagement_rate(obs)
    if engagement is not None:
        return "engagement_rate", engagement
    return "engagement_rate", None


def _impression_volume(obs: PerformanceObservation) -> int:
    candidates = [obs.impressions, obs.views]
    values = [int(v) for v in candidates if v is not None]
    if values:
        return max(values)
    # Fall back to the generic metric value for views/impressions rows.
    if obs.metric_name in {"views", "impressions"}:
        return int(obs.metric_value)
    return 0


def compute_performance_score(obs: PerformanceObservation) -> PerformanceScore:
    """Compute a §25.6 normalized, windowed, confidence-gated PerformanceScore."""
    window = obs.window or "7d"
    primary_metric, raw = _primary_metric_value(obs)
    volume = _impression_volume(obs)

    excluded_reason: str | None = None
    normalized = 0.0 if raw is None else max(0.0, min(1.0, float(raw)))

    # Confidence starts from volume sufficiency and is degraded for early signals
    # and missing primary metrics so we never over-trust raw view counts.
    if raw is None:
        confidence = 0.0
        excluded_reason = "no_normalized_metric"
    elif volume < MIN_CONFIDENT_IMPRESSIONS:
        confidence = 0.2
        excluded_reason = "low_impressions"
    elif window in EARLY_SIGNAL_WINDOWS:
        confidence = 0.4
        excluded_reason = "early_signal_window"
    else:
        # Scale confidence with volume (saturating) for mature windows.
        confidence = min(1.0, 0.6 + 0.4 * min(1.0, volume / (MIN_CONFIDENT_IMPRESSIONS * 10)))

    sample_size = 1 if volume > 0 or raw is not None else 0

    return PerformanceScore(
        id=f"pscore_{obs.id}",
        observation_id=obs.id,
        case_id=obs.case_id,
        video_version_id=obs.video_version_id,
        platform=obs.platform,
        account_id=obs.account_id,
        window=window,
        primary_metric=primary_metric,  # type: ignore[arg-type]
        normalized_score=normalized,
        confidence=confidence,
        sample_size=sample_size,
        excluded_reason=excluded_reason,
    )


def score_is_active_eligible(score: PerformanceScore) -> bool:
    """Whether a score may back an *active* (vs early-signal) memory conclusion."""
    return (
        score.excluded_reason is None
        and score.window in ACTIVE_ELIGIBLE_WINDOWS
        and score.confidence >= MEMORY_ACTIVATION_MIN_CONFIDENCE
    )


def memory_meets_activation_threshold(memory: CaseMemory, *, force: bool = False) -> bool:
    """§25.7: active requires sample_size + confidence thresholds unless forced."""
    if force:
        return True
    return (
        memory.confidence >= MEMORY_ACTIVATION_MIN_CONFIDENCE
        and memory.sample_size >= MEMORY_ACTIVATION_MIN_SAMPLE_SIZE
    )


# ---------------------------------------------------------------------------
# §25.5 Feature extraction
# ---------------------------------------------------------------------------

_HOOK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("question", r"[?？]"),
    ("number", r"\d"),
    ("pain_point", r"痛点|烦恼|难题|problem|struggle"),
    ("contrast", r"但是|然而|没想到|but |however"),
)

_CTA_PATTERNS: tuple[tuple[str, str], ...] = (
    ("link_in_bio", r"主页|简介|link in bio|bio"),
    ("follow", r"关注|follow"),
    ("comment", r"评论|留言|comment"),
    ("buy", r"购买|下单|buy|order|购物车"),
    ("dm", r"私信|私我|dm"),
)


def _first_sentence(text: str) -> str:
    parts = re.split(r"[。!?\n.!?]", text.strip())
    for part in parts:
        if part.strip():
            return part.strip()
    return text.strip()[:80]


def _detect(patterns: Sequence[tuple[str, str]], text: str) -> str | None:
    lowered = text.lower()
    for label, pattern in patterns:
        if re.search(pattern, lowered):
            return label
    return None


def _script_structure(text: str) -> str | None:
    line_count = len([line for line in text.splitlines() if line.strip()])
    if not text.strip():
        return None
    if line_count >= 3:
        return "multi_beat"
    if re.search(r"首先|其次|最后|step", text.lower()):
        return "listicle"
    return "single_beat"


def _topic_tags(text: str, *, extra: Iterable[str] = ()) -> list[str]:
    tags: list[str] = []
    for token in extra:
        token = (token or "").strip()
        if token and token not in tags:
            tags.append(token)
    # Cheap keyword extraction: pick distinct Chinese 2-grams / Latin words.
    words = re.findall(r"[A-Za-z]{4,}|[一-鿿]{2,4}", text)
    for word in words:
        if word not in tags:
            tags.append(word)
        if len(tags) >= 8:
            break
    return tags[:8]


def extract_script_features(
    script: ScriptVersion,
    *,
    case_id: str,
    feature_id: str,
    creative_intent: dict | None = None,
) -> CreativeFeatureVector:
    """ScriptFeatureExtractionNode (§25.5): partial vector from ScriptVersion."""
    text = script.script or ""
    intent = creative_intent or {}
    hook = _detect(_HOOK_PATTERNS, _first_sentence(text)) or "statement"
    cta = _detect(_CTA_PATTERNS, text)
    angle = intent.get("angle") if isinstance(intent, dict) else None
    return CreativeFeatureVector(
        id=feature_id,
        case_id=case_id,
        script_version_id=script.id,
        hook_type=hook,
        script_structure=_script_structure(text),
        topic_tags=_topic_tags(text, extra=[str(intent.get("topic"))] if intent.get("topic") else []),
        cta_type=cta,
        angle=str(angle) if angle else None,
        title_tokens=len(re.findall(r"\S+", script.title or "")),
    )


def extract_video_features(
    video: VideoVersion,
    *,
    feature_id: str,
    partial: CreativeFeatureVector | None = None,
    timeline_plan: dict | None = None,
    style_plan: dict | None = None,
) -> CreativeFeatureVector:
    """VideoFeatureExtractionNode (§25.5): complete vector from VideoVersion + plans."""
    base = partial.model_copy() if partial is not None else CreativeFeatureVector(
        id=feature_id, case_id=video.case_id
    )
    timeline = timeline_plan or {}
    style = style_plan or {}

    segments = timeline.get("segments") if isinstance(timeline, dict) else None
    cuts = len(segments) if isinstance(segments, list) else None
    duration = None
    broll_count = base.broll_count
    material_ids: list[str] = list(base.material_ids)
    if isinstance(segments, list):
        durations = [
            float(seg.get("duration_sec", 0) or 0)
            for seg in segments
            if isinstance(seg, dict)
        ]
        duration = sum(durations) or None
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("kind") == "broll" or seg.get("type") == "broll":
                broll_count += 1
            mat = seg.get("material_id")
            if mat and mat not in material_ids:
                material_ids.append(str(mat))

    cut_density = (cuts / duration) if (cuts and duration) else None
    broll_density = (broll_count / duration) if (broll_count and duration) else None

    update = {
        "id": feature_id,
        "case_id": video.case_id,
        "video_version_id": video.id,
        "script_version_id": base.script_version_id or video.script_version_id,
        "duration_sec": duration if duration is not None else base.duration_sec,
        "cut_density": cut_density if cut_density is not None else base.cut_density,
        "broll_density": broll_density if broll_density is not None else base.broll_density,
        "broll_count": broll_count,
        "material_ids": material_ids,
        "subtitle_style_id": (style.get("subtitle_style_id") if isinstance(style, dict) else None)
        or base.subtitle_style_id,
        "bgm_id": (style.get("bgm_id") if isinstance(style, dict) else None) or base.bgm_id,
        "cover_style": (style.get("cover_style") if isinstance(style, dict) else None)
        or base.cover_style,
    }
    return base.model_copy(update=update)


# ---------------------------------------------------------------------------
# §25.8 Memory recall
# ---------------------------------------------------------------------------

def _within_validity_window(scope: CaseMemoryScope, now: datetime) -> bool:
    if scope.valid_from is not None and _aware(scope.valid_from) > now:
        return False
    if scope.valid_until is not None and _aware(scope.valid_until) < now:
        return False
    return True


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=utcnow().tzinfo)
    return value


def filter_recall_memories(
    memories: Sequence[CaseMemory],
    *,
    mode: MemoryRecallMode = "recent",
    topic: str | None = None,
    platform: str | None = None,
    memory_type: str | None = None,
    scope_key: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
    score_lookup: dict[str, float] | None = None,
) -> list[CaseMemory]:
    """§25.8 retrieval: filter active memories in their validity window, by scope,
    then rank by confidence (or performance score for high/low modes)."""
    now = now or utcnow()
    score_lookup = score_lookup or {}
    candidates: list[CaseMemory] = []
    for memory in memories:
        if memory.status != "active":
            continue
        if not _within_validity_window(memory.scope, now):
            continue
        if scope_key is not None and memory.scope.scope_key not in (scope_key, None):
            continue
        if memory_type is not None and memory.memory_type != memory_type:
            continue
        if platform is not None:
            platforms = memory.scope.applies_to_platforms
            if platforms and platform not in platforms:
                continue
        if topic is not None:
            haystack = " ".join(
                [memory.insight, *memory.scope.applies_to_script_intents]
            ).lower()
            if topic.lower() not in haystack and not any(
                topic.lower() in tag.lower() for tag in memory.scope.applies_to_script_intents
            ):
                continue
        candidates.append(memory)

    if mode == "high_performance":
        candidates.sort(
            key=lambda m: (score_lookup.get(_memory_score_key(m), -1.0), m.confidence),
            reverse=True,
        )
    elif mode == "low_performance":
        candidates.sort(
            key=lambda m: (score_lookup.get(_memory_score_key(m), 2.0), -m.confidence),
        )
    elif mode == "recent":
        candidates.sort(key=lambda m: m.updated_at, reverse=True)
    else:  # topic / platform / memory_type: rank by confidence then recency.
        candidates.sort(key=lambda m: (m.confidence, m.updated_at), reverse=True)

    return candidates[: max(0, limit)]


def _memory_score_key(memory: CaseMemory) -> str:
    return memory.scope.scope_key or memory.id


# ---------------------------------------------------------------------------
# §8.4 HistoricalPerformanceAnalysisNode + CaseReflectionNode
# ---------------------------------------------------------------------------

def analyze_historical_performance(
    observations: Sequence[PerformanceObservation],
    scores: Sequence[PerformanceScore],
) -> list[dict]:
    """Group observations/scores by (platform, account, window) with sample_size.

    §8.4: never treat raw plays as quality; always carry sample_size and keep
    platforms/accounts separated.
    """
    score_by_obs = {s.observation_id: s for s in scores}
    groups: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {
            "platform": None,
            "account_id": None,
            "window": None,
            "observation_ids": [],
            "score_values": [],
            "confident_score_values": [],
        }
    )
    for obs in observations:
        key = (obs.platform or "unknown", obs.account_id or "unknown", obs.window or "unknown")
        group = groups[key]
        group["platform"] = obs.platform
        group["account_id"] = obs.account_id
        group["window"] = obs.window
        group["observation_ids"].append(obs.id)
        score = score_by_obs.get(obs.id)
        if score is not None:
            group["score_values"].append(score.normalized_score)
            if score.excluded_reason is None:
                group["confident_score_values"].append(score.normalized_score)

    analysis: list[dict] = []
    for group in groups.values():
        sample_size = len(group["observation_ids"])
        confident = group["confident_score_values"]
        avg = sum(confident) / len(confident) if confident else None
        analysis.append(
            {
                "platform": group["platform"],
                "account_id": group["account_id"],
                "window": group["window"],
                "sample_size": sample_size,
                "confident_sample_size": len(confident),
                "avg_normalized_score": avg,
                "observation_ids": group["observation_ids"],
            }
        )
    analysis.sort(key=lambda item: (item["avg_normalized_score"] or -1.0), reverse=True)
    return analysis


def build_memory_proposals(
    *,
    case_id: str,
    reflection_run_id: str,
    analysis: Sequence[dict],
    briefs: Sequence[CreativeBrief] = (),
    existing_active: Sequence[CaseMemory] = (),
    existing_proposed: Sequence[CaseMemory] = (),
    id_factory=None,
) -> list[MemoryProposal]:
    """§8.4 derive data-driven memory proposals from performance analysis.

    Each proposal carries evidence_refs (observation ids), confidence, sample_size
    and scope. Dedup against existing active + proposed memories by
    (insight, memory_type, scope_key). Counter-examples (low-performing groups)
    become ``negative_lesson`` proposals.
    """
    if id_factory is None:  # pragma: no cover - exercised via service layer
        from packages.core.storage.repository import new_id

        def id_factory() -> str:
            return new_id("mem")

    existing_keys = {
        (m.insight, m.memory_type, m.scope.scope_key)
        for m in (*existing_active, *existing_proposed)
    }
    brief_topic = briefs[0].topic if briefs else None
    brief_summary = briefs[0].summary if briefs else None

    proposals: list[MemoryProposal] = []
    new_keys: set[tuple] = set()

    for group in analysis:
        avg = group.get("avg_normalized_score")
        confident_n = group.get("confident_sample_size", 0)
        if avg is None or confident_n <= 0:
            # No-silent-degrade: groups without confident scores never become
            # memory; they only inform early-signal review elsewhere.
            continue
        platform = group.get("platform")
        scope_key = platform or group.get("account_id")
        scope = CaseMemoryScope(
            applies_to_platforms=[platform] if platform else [],
            scope_key=scope_key,
        )
        sample_size = int(group.get("sample_size") or confident_n)
        # Counter-examples: groups whose confident scores trail the leader.
        is_negative = avg < 0.35
        memory_type = "negative_lesson" if is_negative else "video_pattern"
        descriptor = brief_topic or brief_summary or "this case"
        if is_negative:
            insight = (
                f"Avoid the under-performing pattern on {platform or 'this channel'} "
                f"for {descriptor} (normalized score {avg:.2f}, n={sample_size})."
            )
        else:
            insight = (
                f"Reuse the top-performing pattern on {platform or 'this channel'} "
                f"for {descriptor} (normalized score {avg:.2f}, n={sample_size})."
            )
        key = (insight, memory_type, scope_key)
        if key in existing_keys or key in new_keys:
            continue
        new_keys.add(key)
        proposals.append(
            MemoryProposal(
                id=id_factory(),
                case_id=case_id,
                status="proposed",
                memory_type=memory_type,  # type: ignore[arg-type]
                scope=scope,
                insight=insight,
                evidence=list(group.get("observation_ids", [])) + [reflection_run_id],
                confidence=round(min(0.95, 0.5 + 0.1 * confident_n), 4),
                sample_size=sample_size,
                proposed_by_reflection_run_id=reflection_run_id,
            )
        )
    return proposals
