"""Real b-roll candidate ranking from AnnotationV4 clips + narration beats.

Replaces the seeded ``score=1`` b-roll pick. For every annotated b-roll asset we
turn its ``ClipV4`` clips into matchable scenes, score each against the script /
narration beats (jieba keyword similarity + clip quality + usage-window
coverage), demote recently-used assets via the selection ledger, and return
ranked candidates carrying real scores + matched keywords. No annotations ->
empty list (the node soft-degrades; never a fabricated pick).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from packages.core.contracts import AnnotationV4, SelectionLedgerEntry
from packages.media.annotation._material import is_video
from packages.planning.material._avoid import avoid_intervals, subtract_bad_spans
from packages.planning.material.keywords import ScriptSegment
from packages.planning.material.matching import BrollScene, best_match
from packages.planning.material.portrait_pack import clip_is_lip_sync_usable
from packages.planning.selection.recency import RecencyConfig, recency_penalty_for

# Score weights (ported from the origin base_score formula, normalized to the
# same shape: semantic match dominates, quality + usage-window coverage refine).
_SEMANTIC_WEIGHT = 100.0
_USAGE_COVER_WEIGHT = 18.0
_QUALITY_WEIGHT = 8.0
_DURATION_WEIGHT = 0.5
_RECENCY_WEIGHT = 12.0
# A candidate must clear a minimal semantic relevance to be offered at all,
# otherwise an unrelated clip would still be picked (the honest alternative to
# the old "anything usable is fine" seed). A clip also has to have *real*
# semantic overlap (``MatchResult.has_overlap``) — the duration-fit tie-breaker
# alone never makes an unrelated clip relevant.
_MIN_SIMILARITY = 0.05
_MIN_CLEAN_SPAN_SEC = 1.0

# Subject-type surface forms that mark a clip as person-centric (a presenter /
# talking-head / 出镜人物). Substring match against ``ClipSemanticsV4.subject_type``.
_PERSON_SUBJECT_TERMS = (
    "presenter",
    "salesperson",
    "spokes",
    "host",
    "anchor",
    "streamer",
    "influencer",
    "person",
    "people",
    "human",
    "speaker",
    "主播",
    "真人",
    "导购",
    "模特",
    "口播",
    "出镜",
    "人物",
    "讲解",
)


def clip_shows_person(clip) -> bool:
    """Whether a clip features a real person as its subject — a presenter /
    talking-head / on-camera 出镜人物 — making it unsuitable as clean b-roll cover.

    A "B-roll only" video must carry scene/product footage, not a person. A
    talking-head clip that *is* lip-sync usable is already routed to the A-roll
    pool upstream; this predicate is the content-based backstop for person clips
    that are NOT lip-sync usable (multi-face, presenter wide shots) and would
    otherwise leak into b-roll. It looks only at clip *content* — subject type,
    an explicit face flag, multiple faces (>=2), or talking-head visual cues — and
    deliberately ignores the ``recommended_for_lip_sync`` usage flag (a noisy
    legacy-b-roll signal handled by the A-roll split). A single *incidental*
    background face (``face_count_max == 1`` with no person subject) is NOT a
    person clip, so legitimate scene/product cover that merely catches one face in
    frame still qualifies as b-roll.
    """
    sem = clip.semantics
    subject = (sem.subject_type or "").lower()
    if any(term in subject for term in _PERSON_SUBJECT_TERMS):
        return True
    if sem.contains_face is True:
        return True
    fcm = sem.face_count_max
    if sem.contains_face is not False and fcm is not None and fcm >= 2:
        return True
    if sem.mouth_moving is True or sem.gaze_to_camera is True:
        return True
    return False


@dataclass(frozen=True)
class BrollCandidate:
    asset_id: str
    clip_id: str
    score: float
    base_score: float
    recency_penalty: float
    matched_keywords: tuple[str, ...]
    scene_name: str
    source_start: float
    source_end: float
    diversity_key: str = ""
    best_segment: ScriptSegment | None = field(default=None)


def _scene_from_clip(
    asset_id: str, clip, span: tuple[float, float] | None = None
) -> BrollScene:
    semantics = clip.semantics
    retrieval = clip.retrieval
    start, end = span if span is not None else (clip.start, clip.end)
    name = (
        semantics.narrative_role
        or semantics.action
        or semantics.scene_type
        or retrieval.summary
        or "片段"
    ).strip()[:48] or "片段"
    description = (retrieval.retrieval_sentence or retrieval.summary or "").strip()
    keywords = tuple(kw.strip() for kw in retrieval.keywords if kw.strip())
    return BrollScene(
        clip_id=clip.segment_id,
        name=name,
        description=description,
        keywords=keywords,
        start=float(start),
        end=float(end),
    )


def _usage_cover_ratio(
    annotation: AnnotationV4, clip, span: tuple[float, float] | None = None
) -> float:
    """Fraction of the candidate span overlapped by a recommended usage window."""
    if not annotation.usage_windows:
        return 0.0
    start, end = span if span is not None else (clip.start, clip.end)
    span_len = max(0.0, float(end) - float(start))
    if span_len <= 0:
        return 0.0
    covered = 0.0
    for win in annotation.usage_windows:
        lo = max(float(start), float(win.start))
        hi = min(float(end), float(win.end))
        if hi > lo:
            covered += hi - lo
    return min(1.0, covered / span_len)


def _diversity_key(clip) -> str:
    return (clip.semantics.scene_type or clip.semantics.narrative_role or "").strip()


def rank_broll_candidates(
    *,
    annotations: dict[str, AnnotationV4],
    asset_kinds: dict[str, str] | None = None,
    segments: Sequence[ScriptSegment],
    ledger_entries: Sequence[SelectionLedgerEntry] = (),
    recency_cfg: RecencyConfig | None = None,
    include_generic_coverage: bool = False,
) -> list[BrollCandidate]:
    """Rank b-roll clips across annotated assets against the script beats.

    ``annotations`` maps asset_id -> AnnotationV4 (annotated b-roll/video assets).
    ``ledger_entries`` are this case's recent b-roll selections (most-recent
    first); a previously-picked asset/cluster is demoted. Returns candidates
    sorted by final score descending. Empty when nothing clears the relevance
    floor unless generic coverage is enabled for full-coverage flows.
    """
    seg_list = list(segments)
    candidates: list[BrollCandidate] = []
    for asset_id, annotation in annotations.items():
        material_type = (asset_kinds or {}).get(asset_id) or annotation.meta.material_type
        from_unified_video = is_video(material_type)
        bad_spans = avoid_intervals(annotation)
        for clip in annotation.clips:
            # Unified-video B-roll must be person-free cover footage.
            if clip.usage.role.value == "avoid":
                continue
            if from_unified_video and clip_is_lip_sync_usable(clip):
                continue
            if clip_shows_person(clip):
                continue
            clean_spans = subtract_bad_spans(
                clip.start,
                clip.end,
                bad_spans,
                min_len=_MIN_CLEAN_SPAN_SEC,
            )
            for span_index, clean_span in enumerate(clean_spans):
                scene = _scene_from_clip(asset_id, clip, span=clean_span)
                candidate_clip_id = _clip_id_for_clean_span(clip.segment_id, span_index)
                best_segment, match = best_match(seg_list, scene)
                if not match.has_overlap or match.similarity < _MIN_SIMILARITY:
                    if include_generic_coverage:
                        candidates.append(
                            _generic_coverage_candidate(
                                asset_id=asset_id,
                                annotation=annotation,
                                clip=clip,
                                clip_id=candidate_clip_id,
                                scene=scene,
                                # A clip with no real overlap only "matched" via the
                                # duration-fit tie-breaker, which points every such clip
                                # at the same first beat. Drop that pseudo-anchor so the
                                # planner can spread generic fillers across the timeline;
                                # keep a real (if weak, sub-floor) overlap anchor.
                                best_segment=best_segment if match.has_overlap else None,
                                ledger_entries=ledger_entries,
                                recency_cfg=recency_cfg,
                            )
                        )
                    continue
                usage_cover = _usage_cover_ratio(annotation, clip, span=clean_span)
                quality = float(annotation.quality_report.get("usable_ratio") or 0.5)
                duration = max(0.0, scene.end - scene.start)
                base = (
                    match.similarity * _SEMANTIC_WEIGHT
                    + usage_cover * _USAGE_COVER_WEIGHT
                    + quality * _QUALITY_WEIGHT
                    + min(duration, 8.0) * _DURATION_WEIGHT
                )
                diversity_key = _diversity_key(clip)
                penalty = recency_penalty_for(
                    ledger_entries,
                    asset_id=asset_id,
                    diversity_key=diversity_key,
                    cfg=recency_cfg,
                )
                final = max(0.0, base - penalty * _RECENCY_WEIGHT)
                candidates.append(
                    BrollCandidate(
                        asset_id=asset_id,
                        clip_id=candidate_clip_id,
                        score=round(final, 3),
                        base_score=round(base, 3),
                        recency_penalty=round(penalty, 3),
                        matched_keywords=match.matched_keywords,
                        scene_name=scene.name,
                        source_start=round(scene.start, 3),
                        source_end=round(scene.end, 3),
                        diversity_key=diversity_key,
                        best_segment=best_segment,
                    )
                )

    candidates.sort(key=lambda c: _recent_reuse_sort_key(c, ledger_entries))
    return candidates


def _clip_id_for_clean_span(segment_id: str, span_index: int) -> str:
    if span_index == 0:
        return segment_id
    return f"{segment_id}-m{span_index}"


def _recent_reuse_sort_key(
    candidate: BrollCandidate, ledger_entries: Sequence[SelectionLedgerEntry]
) -> tuple[int, int, int, int, int, float, str, str]:
    """Keep recent b-roll reuse behind fresh clips for coverage planning."""
    rank = 0
    exact_count = 0
    asset_count = 0
    cluster_count = 0
    most_recent_pos: int | None = None
    for pos, entry in enumerate(ledger_entries):
        if entry.medium != "broll":
            continue
        same_asset = entry.asset_id == candidate.asset_id
        same_clip = same_asset and bool(entry.clip_id) and entry.clip_id == candidate.clip_id
        same_cluster = bool(candidate.diversity_key) and entry.diversity_key == candidate.diversity_key
        if same_clip:
            exact_count += 1
            asset_count += 1
            rank = max(rank, 3)
            if most_recent_pos is None:
                most_recent_pos = pos
        elif same_asset:
            asset_count += 1
            rank = max(rank, 2)
            if most_recent_pos is None:
                most_recent_pos = pos
        elif same_cluster:
            cluster_count += 1
            rank = max(rank, 1)
            if most_recent_pos is None:
                most_recent_pos = pos
    last_used_sort = -(most_recent_pos if most_recent_pos is not None else len(ledger_entries) + 1)
    return (
        rank,
        exact_count,
        asset_count,
        cluster_count,
        last_used_sort,
        -candidate.score,
        candidate.asset_id,
        candidate.clip_id,
    )


def _generic_coverage_candidate(
    *,
    asset_id: str,
    annotation: AnnotationV4,
    clip,
    clip_id: str,
    scene: BrollScene,
    best_segment: ScriptSegment | None,
    ledger_entries: Sequence[SelectionLedgerEntry],
    recency_cfg: RecencyConfig | None,
) -> BrollCandidate:
    usage_cover = _usage_cover_ratio(annotation, clip, span=(scene.start, scene.end))
    quality = float(annotation.quality_report.get("usable_ratio") or 0.5)
    duration = max(0.0, scene.end - scene.start)
    base = 12.0 + usage_cover * 8.0 + quality * 8.0 + min(duration, 8.0) * _DURATION_WEIGHT
    diversity_key = _diversity_key(clip)
    penalty = recency_penalty_for(
        ledger_entries,
        asset_id=asset_id,
        diversity_key=diversity_key,
        cfg=recency_cfg,
    )
    final = max(0.0, base - penalty * _RECENCY_WEIGHT)
    return BrollCandidate(
        asset_id=asset_id,
        clip_id=clip_id,
        score=round(final, 3),
        base_score=round(base, 3),
        recency_penalty=round(penalty, 3),
        matched_keywords=(),
        scene_name=scene.name,
        source_start=round(scene.start, 3),
        source_end=round(scene.end, 3),
        diversity_key=diversity_key,
        best_segment=best_segment,
    )
