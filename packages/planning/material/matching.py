"""Pure keyword-similarity matching between script beats and b-roll clips.

Ported from the origin ``_calculate_segment_similarity`` /
``_calculate_semantic_bonus``: a Jaccard-variant over jieba keyword sets plus
deterministic bonuses (scene-name hit, description-keyword hits, a small
synonym-expansion bonus, ideal-duration bonus). All inputs are plain values
derived from ``AnnotationV4`` clips, so the function is pure and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.planning.material.keywords import ScriptSegment, extract_keywords

# Synonym expansion (kept from the origin). Maps a scene keyword to script-side
# surface forms that should still count as a soft semantic hit.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "打磨": ("研磨", "砂光", "抛光", "修整"),
    "喷涂": ("喷漆", "涂装", "上漆", "上色"),
    "补漆": ("喷漆", "修复", "修补", "处理划痕"),
    "抛光": ("打蜡", "研磨", "镜面处理"),
    "效果": ("结果", "成果", "展示", "呈现"),
    "产品": ("商品", "服务", "项目", "解决方案"),
    "介绍": ("讲解", "说明", "展示", "推荐"),
    "客户": ("顾客", "用户", "车主", "消费者"),
    "专业": ("专注", "熟练", "经验丰富", "技术好"),
}


@dataclass(frozen=True)
class BrollScene:
    """A matchable b-roll scene distilled from one ``ClipV4``.

    ``name`` is the human label (narrative_role / action / scene_type),
    ``description`` the retrieval summary, ``keywords`` the clip retrieval
    keywords plus scene-derived tokens. ``start``/``end`` are the clip's
    in-source window (seconds).
    """

    clip_id: str
    name: str
    description: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)
    start: float = 0.0
    end: float = 0.0


@dataclass(frozen=True)
class MatchResult:
    similarity: float
    matched_keywords: tuple[str, ...]
    # True when the match rests on real semantic overlap (keyword intersection,
    # scene-name/keyword text hit, or synonym expansion) rather than only the
    # duration-fit tie-breaker. Callers gate relevance on this so an unrelated
    # clip is never offered just because its length is convenient.
    has_overlap: bool = False


def _scene_keyword_set(scene: BrollScene) -> set[str]:
    derived = extract_keywords(f"{scene.name} {scene.description}".strip())
    return {kw for kw in (*derived, *scene.keywords) if kw}


def _semantic_bonus(script_text: str, scene_keywords: set[str]) -> float:
    bonus = 0.0
    for kw in scene_keywords:
        for synonym in _SYNONYMS.get(kw, ()):  # only known scene keywords expand
            if synonym in script_text:
                bonus += 0.1
                break
    return min(bonus, 0.3)


def score_segment(segment: ScriptSegment, scene: BrollScene) -> MatchResult:
    """Score how well ``scene`` matches ``segment`` (0..1) + the matched keywords.

    Jaccard-variant: intersection over the larger keyword set, plus bonuses for
    a direct scene-name hit, any scene keyword appearing in the beat text,
    description-keyword hit count, synonym expansion, and an ideal clip duration
    (2..8s). Returns the keyword intersection so callers can populate
    ``matched_keywords``.
    """
    script_kw = set(segment.keywords)
    scene_kw = _scene_keyword_set(scene)
    common = script_kw & scene_kw

    if not script_kw and not scene_kw:
        return MatchResult(0.0, (), has_overlap=False)

    similarity = len(common) / max(len(script_kw), len(scene_kw), 1)
    has_overlap = bool(common)

    if scene.name and scene.name in segment.text:
        similarity += 0.35
        has_overlap = True
    for kw in scene_kw:
        if len(kw) >= 2 and kw in segment.text:
            similarity += 0.15
            has_overlap = True
            break
    desc_hits = sum(1 for kw in scene_kw if kw in segment.text)
    if desc_hits:
        similarity += min(desc_hits * 0.08, 0.25)
        has_overlap = True
    semantic = _semantic_bonus(segment.text, scene_kw)
    if semantic > 0:
        similarity += semantic
        has_overlap = True

    # Duration fit is a tie-breaker only — it does NOT make an unrelated clip
    # relevant, so it never sets has_overlap.
    duration = max(0.0, scene.end - scene.start)
    if 2.0 <= duration <= 8.0:
        similarity += 0.05

    ordered = tuple(kw for kw in segment.keywords if kw in common)
    return MatchResult(min(similarity, 1.0), ordered, has_overlap=has_overlap)


def best_match(segments: list[ScriptSegment], scene: BrollScene) -> tuple[ScriptSegment | None, MatchResult]:
    """Return the best-scoring script beat for ``scene`` and its match result."""
    best_segment: ScriptSegment | None = None
    best = MatchResult(0.0, (), has_overlap=False)
    for segment in segments:
        result = score_segment(segment, scene)
        if result.similarity > best.similarity:
            best = result
            best_segment = segment
    return best_segment, best
