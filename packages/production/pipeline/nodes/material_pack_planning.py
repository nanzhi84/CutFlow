"""MaterialPackPlanning node: rank usable portrait/b-roll/bgm/font candidates.

Real ranking (no seeded ``score=1``): portrait/bgm/font score on availability +
annotated lip-sync suitability + a recency demotion from the selection ledger;
b-roll candidates are matched against the script beats from their real
``AnnotationV4`` clips (jieba keyword similarity + usage-window coverage). When a
b-roll asset has no real annotation it yields no candidate (the BrollPlanning
node then soft-degrades — honest, never a fabricated pick).
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind
from packages.core.contracts.artifacts import MaterialCandidate, MaterialPackArtifact
from packages.core.workflow import NodeOutput
from packages.planning.material import (
    avoid_intervals,
    clip_is_lip_sync_usable,
    clip_shows_person,
    clean_portrait_source_windows,
    extract_keywords,
    rank_broll_candidates,
    rank_portrait_clip_candidates,
    score_simple_candidate,
    segment_script,
    subtract_bad_spans,
)
from packages.planning.material.broll_pack import _MIN_CLEAN_SPAN_SEC
from packages.planning.selection.recency import recency_penalty_for
from packages.planning.selection.recency_context import (
    build_portrait_recency_context_from_ledger,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._broll_policy import broll_generic_coverage_enabled

_BROLL_RECENT_SELECTION_LIMIT = 80
_PORTRAIT_MIN_CLEAN_SPAN_SEC = 0.08
_BGM_BASE_SCORE = 70.0
_BGM_ENERGY_WEIGHT = 5.0
_BGM_CONFIDENCE_WEIGHT = 5.0
_BGM_RECENCY_WEIGHT = 12.0
_BGM_TOP_K = 8


def run(ctx: NodeContext) -> NodeOutput:
    request = ctx.state.request
    repo = ctx.repository
    assets = list(repo.media_assets.values())

    # Visual assets are one unified ``video`` bucket (#99/#129/#133); A-roll vs
    # B-roll is a per-clip AnnotationV4 decision, not an ``asset.kind`` split. The
    # legacy ``portrait``/``broll`` kinds no longer exist (migration 0026/0033).
    visual_material_kinds = {"video"}

    def _is_ai_reference(asset) -> bool:
        # AI素材 (Seedance reference uploads) are case-scoped media assets tagged
        # ai_material. They must NEVER enter the digital-human / b-roll material
        # pools — they are reference inputs for generation, not footage to cut in.
        return "ai_material" in (getattr(asset, "tags", None) or [])

    def _eligible(asset, kind: str) -> bool:
        return (
            asset.usable
            and asset.kind == kind
            and asset.case_id in {None, request.case_id}
            and not _is_ai_reference(asset)
        )

    def _eligible_visual(asset) -> bool:
        return (
            asset.usable
            and asset.kind in visual_material_kinds
            and asset.case_id in {None, request.case_id}
            and not _is_ai_reference(asset)
        )

    portrait_reserved = _active_reserved_asset_ids(
        repo, case_id=request.case_id, run_id=ctx.run.id, medium="portrait"
    )
    broll_reserved = _active_reserved_asset_ids(
        repo, case_id=request.case_id, run_id=ctx.run.id, medium="broll"
    )
    bgm_reserved = _active_reserved_asset_ids(
        repo, case_id=request.case_id, run_id=ctx.run.id, medium="bgm"
    )
    font_reserved = _active_reserved_asset_ids(
        repo, case_id=request.case_id, run_id=ctx.run.id, medium="font"
    )

    # Unified visual planning: every visual asset is split per clip into A-roll
    # (lip-sync-usable) vs B-roll (cover/backup) through one ranking path.
    visual_assets = [asset for asset in assets if _eligible_visual(asset)]
    portrait_visual_assets = [
        asset
        for asset in visual_assets
        if asset.id not in portrait_reserved
    ]
    broll_visual_assets = [
        asset
        for asset in visual_assets
        if (request.broll.case_id is None or asset.case_id == request.broll.case_id)
        and asset.id not in broll_reserved
    ]
    bgm_assets = [asset for asset in assets if _eligible(asset, "bgm") and asset.id not in bgm_reserved]
    font_assets = [
        asset for asset in assets if _eligible(asset, "font") and asset.id not in font_reserved
    ]

    # --- portrait (coverage is enforced later; here: lip-sync + recency) ------
    portrait_ledger = repo.recent_selections(case_id=request.case_id, medium="portrait")
    portrait_candidates: list[MaterialCandidate] = []
    # Clip-level lip-sync candidates from the unified visual bucket: one candidate per
    # usable talking-head clip, carrying its source window so PortraitPlanning cuts the
    # exact clip span. Coverage/capacity is still gated downstream.
    portrait_annotations = {
        asset.id: annotation
        for asset in portrait_visual_assets
        if (annotation := repo.annotation_v4_for_asset(asset.id)) is not None
    }
    portrait_avoid_cache: dict[str, list[tuple[float, float]]] = {}
    # Weighted recency + opening-guard context is the single ledger-derived signal the
    # downstream PortraitPlanning boundary planner consumes; computing it HERE (the one
    # node that reads the ledger) and stamping the full ``recent_usage`` dict onto each
    # candidate's open metadata is what lets PortraitPlanning stop reading the ledger
    # itself. It depends only on ``asset_id`` (template identity), so it is cached per
    # asset and shared across that asset's clip windows — identical to the value the
    # old per-window recompute produced.
    portrait_recent_usage_cache: dict[str, dict] = {}
    for clip_candidate in rank_portrait_clip_candidates(
        annotations=portrait_annotations,
        ledger_entries=portrait_ledger,
    ):
        avoid = portrait_avoid_cache.get(clip_candidate.asset_id)
        if avoid is None:
            avoid = avoid_intervals(portrait_annotations[clip_candidate.asset_id])
            portrait_avoid_cache[clip_candidate.asset_id] = avoid
        source_duration = _source_duration_for_asset(
            ctx,
            asset_id=clip_candidate.asset_id,
            annotation=portrait_annotations[clip_candidate.asset_id],
        )
        clean_windows = clean_portrait_source_windows(
            {
                "source_start": clip_candidate.source_start,
                "source_end": clip_candidate.source_end,
                "avoid_spans": avoid,
            },
            source_duration=source_duration,
        )
        if not clean_windows:
            continue
        recent_usage = portrait_recent_usage_cache.get(clip_candidate.asset_id)
        if recent_usage is None:
            recent_usage = build_portrait_recency_context_from_ledger(
                entries=portrait_ledger,
                template_id=clip_candidate.asset_id,
                diversity_key=None,
            )
            portrait_recent_usage_cache[clip_candidate.asset_id] = recent_usage
        for clean_index, (clean_start, clean_end) in enumerate(clean_windows):
            source_window_id = (
                clip_candidate.clip_id
                if clean_index == 0
                else f"{clip_candidate.clip_id}:m{clean_index}"
            )
            clean_duration = round(clean_end - clean_start, 3)
            portrait_candidates.append(
                MaterialCandidate(
                    asset_id=clip_candidate.asset_id,
                    score=clip_candidate.score,
                    reason=clip_candidate.reason,
                    metadata={
                        "base_score": clip_candidate.base_score,
                        "recency_penalty": clip_candidate.recency_penalty,
                        "clip_id": clip_candidate.clip_id,
                        "source_window_id": source_window_id,
                        "source_start": round(clean_start, 3),
                        "source_end": round(clean_end, 3),
                        "duration": clean_duration,
                        "recent_usage": recent_usage,
                    },
                )
            )
    portrait_candidates.sort(
        key=lambda c: (
            -c.score,
            c.asset_id,
            str((c.metadata or {}).get("clip_id") or ""),
            float((c.metadata or {}).get("source_start") or 0.0),
        )
    )
    _portrait_from_video_count = sum(
        1 for c in portrait_candidates if (c.metadata or {}).get("clip_id")
    )
    portrait_motion_excluded = _portrait_motion_excluded_count(portrait_annotations)

    # --- b-roll (real annotation matching; no annotation -> no candidate) -----
    keywords = extract_keywords(request.script)
    segments = segment_script(request.script, keywords=keywords)
    broll_ledger = repo.recent_selections(
        case_id=request.case_id,
        medium="broll",
        limit=_BROLL_RECENT_SELECTION_LIMIT,
    )
    broll_annotations = {
        asset.id: annotation
        for asset in broll_visual_assets
        if (annotation := repo.annotation_v4_for_asset(asset.id)) is not None
    }
    # Person-centric clips (presenter / talking-head / 出镜人物) that are not
    # lip-sync usable are kept OUT of the b-roll pool — they belong to neither A-roll
    # nor clean cover. Count them so a sparse/empty b-roll plan reads as "person
    # clips were filtered", not as an annotation error.
    broll_person_excluded = sum(
        1
        for annotation in broll_annotations.values()
        for clip in annotation.clips
        if clip.usage.role.value != "avoid"
        and not clip_is_lip_sync_usable(clip)
        and clip_shows_person(clip)
    )
    broll_motion_excluded = _broll_motion_excluded_count(
        broll_annotations,
    )
    broll_candidates: list[MaterialCandidate] = []
    for candidate in rank_broll_candidates(
        annotations=broll_annotations,
        segments=segments,
        ledger_entries=broll_ledger,
        include_generic_coverage=broll_generic_coverage_enabled(request),
    ):
        broll_candidates.append(
            MaterialCandidate(
                asset_id=candidate.asset_id,
                score=candidate.score,
                reason=(
                    f"matched '{candidate.scene_name}' (base {candidate.base_score})"
                    + ("; recently used" if candidate.recency_penalty else "")
                ),
                metadata={
                    "clip_id": candidate.clip_id,
                    "matched_keywords": list(candidate.matched_keywords),
                    "scene_name": candidate.scene_name,
                    "source_start": candidate.source_start,
                    "source_end": candidate.source_end,
                    "base_score": candidate.base_score,
                    "recency_penalty": candidate.recency_penalty,
                    # Carry the recency cluster so the b-roll planning nodes can re-apply
                    # this MaterialPack-computed recency penalty by (asset_id, clip_id) /
                    # (asset_id, diversity_key) without reading the ledger themselves.
                    "diversity_key": candidate.diversity_key,
                },
            )
        )

    # --- bgm / font (availability + recency) ---------------------------------
    bgm_ledger = repo.recent_selections(case_id=request.case_id, medium="bgm")
    font_ledger = repo.recent_selections(case_id=request.case_id, medium="font")
    bgm_annotations = {
        asset.id: annotation
        for asset in bgm_assets
        if (annotation := repo.annotation_v4_for_asset(asset.id)) is not None
    }
    bgm_candidates = _limit_bgm_candidates(
        _bgm_segment_candidates(bgm_assets, bgm_annotations, bgm_ledger),
        limit=_BGM_TOP_K,
        requested_asset_id=request.bgm.bgm_id,
    )
    font_candidates = _simple_candidates(font_assets, "font", font_ledger)

    # §6.6 reserve: claim a TTL lease over each top candidate per medium so a
    # concurrent same-case run does not silently collide on the same asset. The
    # per-medium production node commits the asset it actually ships; cancel/failure
    # releases the rest. Assets a live run already holds were filtered before ranking;
    # the reservation ids surfaced here are the ones THIS run owns, wiring the
    # previously-stubbed ``reservations`` contract field for real.
    reservation_ids = _reserve_top_candidates(
        ctx,
        case_id=request.case_id,
        portrait_candidates=portrait_candidates,
        broll_candidates=broll_candidates,
        bgm_candidates=bgm_candidates,
        font_candidates=font_candidates,
    )

    payload = MaterialPackArtifact(
        case_id=request.case_id,
        portrait_candidates=portrait_candidates,
        broll_candidates=broll_candidates,
        bgm_candidates=bgm_candidates,
        font_candidates=font_candidates,
        diagnostics={
            "portrait_missing": not portrait_candidates,
            "broll_missing": request.broll.enabled and not broll_candidates,
            "broll_unannotated": request.broll.enabled
            and bool(broll_visual_assets)
            and not broll_annotations,
            "broll_person_excluded": broll_person_excluded,
            "broll_motion_excluded": broll_motion_excluded,
            "portrait_motion_excluded": portrait_motion_excluded,
            "bgm_missing": request.bgm.enabled and not bgm_candidates,
            "portrait_active_reservations": len(portrait_reserved),
            "broll_active_reservations": len(broll_reserved),
            "bgm_active_reservations": len(bgm_reserved),
            "font_active_reservations": len(font_reserved),
            # Unified video bucket visibility: how many portrait candidates came from
            # per-clip lip-sync windows, and the honest "operator uploaded visual
            # material but it has no talking-head clip" signal (an A-roll-insufficiency
            # early warning; PortraitPlanning still enforces the hard coverage gate
            # downstream). Key names stay stable for downstream consumers.
            "portrait_from_video": _portrait_from_video_count,
            "video_no_lipsync": bool(portrait_visual_assets) and _portrait_from_video_count == 0,
        },
        reservations=reservation_ids,
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[
            ctx.artifact(ArtifactKind.plan_material_pack, payload, "MaterialPackPlanArtifact.v1")
        ]
    )


# How many top-ranked candidates to reserve per medium. Reserving the shortlist (not
# only the single eventual pick) is intentional: the production node may pick any of the
# top candidates, and uncommitted reservations are released at finalize/failure.
_RESERVE_TOP_N = 3


def _active_reserved_asset_ids(repo, *, case_id: str, run_id: str, medium: str) -> set[str]:
    return {
        reservation.asset_id
        for reservation in repo.active_selection_reservations(
            case_id=case_id,
            medium=medium,
            exclude_run_id=run_id,
        )
    }


def _source_duration_for_asset(ctx: NodeContext, *, asset_id: str, annotation) -> float | None:
    source = ctx.source_artifact_for_asset(asset_id)
    media_info = getattr(source, "media_info", None)
    if media_info is not None:
        try:
            duration = float(media_info.duration_sec or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            return duration
    try:
        duration = float(getattr(getattr(annotation, "meta", None), "duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _broll_motion_excluded_count(annotations) -> int:
    excluded = 0
    for asset_id, annotation in annotations.items():
        bad_spans = avoid_intervals(annotation)
        if not bad_spans:
            continue
        for clip in annotation.clips:
            if clip.usage.role.value == "avoid":
                continue
            if clip_is_lip_sync_usable(clip):
                continue
            if clip_shows_person(clip):
                continue
            if not _clip_overlaps_bad_span(clip, bad_spans):
                continue
            clean_spans = subtract_bad_spans(
                clip.start,
                clip.end,
                bad_spans,
                min_len=_MIN_CLEAN_SPAN_SEC,
            )
            original_span = (round(float(clip.start), 3), round(float(clip.end), 3))
            if not clean_spans or clean_spans != [original_span]:
                excluded += 1
    return excluded


def _portrait_motion_excluded_count(annotations) -> int:
    excluded = 0
    for annotation in annotations.values():
        bad_spans = avoid_intervals(annotation)
        if not bad_spans:
            continue
        for clip in annotation.clips:
            if clip.usage.role.value == "avoid":
                continue
            if not clip_is_lip_sync_usable(clip):
                continue
            if not _clip_overlaps_bad_span(clip, bad_spans):
                continue
            clean_spans = subtract_bad_spans(
                clip.start,
                clip.end,
                bad_spans,
                min_len=_PORTRAIT_MIN_CLEAN_SPAN_SEC,
            )
            original_span = (round(float(clip.start), 3), round(float(clip.end), 3))
            if not clean_spans or clean_spans != [original_span]:
                excluded += 1
    return excluded


def _clip_overlaps_bad_span(clip, bad_spans: list[tuple[float, float]]) -> bool:
    start = round(float(clip.start), 3)
    end = round(float(clip.end), 3)
    return any(min(end, bad_end) > max(start, bad_start) for bad_start, bad_end in bad_spans)


def _reserve_top_candidates(
    ctx: NodeContext,
    *,
    case_id: str,
    portrait_candidates: list[MaterialCandidate],
    broll_candidates: list[MaterialCandidate],
    bgm_candidates: list[MaterialCandidate],
    font_candidates: list[MaterialCandidate],
) -> list[str]:
    reservation_ids: list[str] = []
    for medium, candidates in (
        ("portrait", portrait_candidates),
        ("broll", broll_candidates),
        ("bgm", bgm_candidates),
        ("font", font_candidates),
    ):
        reserve_candidates = candidates if medium == "bgm" else candidates[:_RESERVE_TOP_N]
        asset_ids = list(dict.fromkeys(c.asset_id for c in reserve_candidates if c.asset_id))
        if not asset_ids:
            continue
        diversity_keys = {
            c.asset_id: (c.metadata or {}).get("diversity_key")
            for c in reserve_candidates
            if c.asset_id
        }
        owned = ctx.repository.reserve_selections(
            case_id=case_id,
            run_id=ctx.run.id,
            medium=medium,
            asset_ids=asset_ids,
            diversity_keys=diversity_keys,
        )
        reservation_ids.extend(reservation.id for reservation in owned)
    return reservation_ids


def _simple_candidates(assets, medium_label, ledger_entries) -> list[MaterialCandidate]:
    candidates: list[MaterialCandidate] = []
    for asset in assets:
        scored = score_simple_candidate(
            asset_id=asset.id, medium_label=medium_label, ledger_entries=ledger_entries
        )
        candidates.append(
            MaterialCandidate(
                asset_id=scored.asset_id,
                score=scored.score,
                reason=scored.reason,
                metadata={
                    "base_score": scored.base_score,
                    "recency_penalty": scored.recency_penalty,
                },
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.asset_id))
    return candidates


def _bgm_segment_candidates(assets, annotations, ledger_entries) -> list[MaterialCandidate]:
    candidates: list[MaterialCandidate] = []
    for asset in assets:
        annotation = annotations.get(asset.id)
        if annotation is None or not annotation.bgm_segments:
            continue
        for segment in annotation.bgm_segments:
            segment_id = str(segment.segment_id or "").strip()
            if not segment_id:
                continue
            source_start = round(float(segment.start), 3)
            source_end = round(float(segment.end), 3)
            duration = round(max(0.0, source_end - source_start), 3)
            if duration <= 0:
                continue
            penalty = recency_penalty_for(
                ledger_entries,
                asset_id=asset.id,
                clip_id=segment_id,
            )
            base = (
                _BGM_BASE_SCORE
                + float(segment.energy or 0.0) * _BGM_ENERGY_WEIGHT
                + float(segment.confidence or 0.0) * _BGM_CONFIDENCE_WEIGHT
            )
            final = max(0.0, base - penalty * _BGM_RECENCY_WEIGHT)
            role = segment.role.value if hasattr(segment.role, "value") else str(segment.role)
            reason = segment.reason or f"BGM segment {duration:.1f}s"
            if penalty > 0:
                reason += "; recently used (demoted)"
            candidates.append(
                MaterialCandidate(
                    asset_id=asset.id,
                    score=round(final, 3),
                    reason=reason,
                    metadata={
                        "base_score": round(base, 3),
                        "recency_penalty": round(penalty, 3),
                        "clip_id": segment_id,
                        "source_start": source_start,
                        "source_end": source_end,
                        "duration": duration,
                        "role": role,
                        "section_type": (
                            segment.section_type.value
                            if hasattr(segment.section_type, "value")
                            else str(segment.section_type)
                        ),
                        "section_label": segment.section_label,
                        "repeat_group": segment.repeat_group,
                        "loopable": bool(segment.loopable),
                        "energy_profile": (
                            segment.energy_profile.value
                            if hasattr(segment.energy_profile, "value")
                            else str(segment.energy_profile)
                        ),
                        "drop_anchor_sec": segment.drop_anchor_sec,
                        "energy": float(segment.energy or 0.0),
                        "mood": segment.mood,
                        "script_fit": list(segment.script_fit),
                        "avoid_script": list(segment.avoid_script),
                        "scene_fit": list(segment.scene_fit),
                        "avoid_scene": list(segment.avoid_scene),
                        "reason": segment.reason,
                        "confidence": float(segment.confidence or 0.0),
                    },
                )
            )
    candidates.sort(
        key=lambda c: (
            -c.score,
            c.asset_id,
            float((c.metadata or {}).get("source_start") or 0.0),
            str((c.metadata or {}).get("clip_id") or ""),
        )
    )
    return candidates


def _limit_bgm_candidates(
    candidates: list[MaterialCandidate],
    *,
    limit: int,
    requested_asset_id: str | None,
) -> list[MaterialCandidate]:
    if limit <= 0 or len(candidates) <= limit:
        return candidates
    top = list(candidates[:limit])
    requested = str(requested_asset_id or "").strip()
    if requested and not any(candidate.asset_id == requested for candidate in top):
        requested_candidate = next(
            (candidate for candidate in candidates[limit:] if candidate.asset_id == requested),
            None,
        )
        if requested_candidate is not None:
            top[-1] = requested_candidate
    return top
