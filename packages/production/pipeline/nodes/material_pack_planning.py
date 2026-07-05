"""MaterialPackPlanning node: build hard-eligible material candidate pools.

Visual MaterialPack candidates are an eligibility boundary, not a retrieval or
ranking boundary: portrait and B-roll pools expose every clip/source span that
passes hard gates (usable asset, annotation, role, lipsync/person split, clean
source span, active reservations). Semantic matching, topK retrieval, and final
assignment happen downstream; recency is carried only as metadata for those
later planners. When annotated visual material is absent or rejected, the node
emits diagnostics instead of fabricating picks.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from typing import Any

from packages.core.contracts import ArtifactKind
from packages.core.contracts.artifacts import MaterialCandidate, MaterialPackArtifact
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing.frame_grid import frame_index
from packages.planning.material import (
    avoid_intervals,
    clip_is_lip_sync_usable,
    clip_shows_person,
    clean_portrait_source_windows,
    subtract_bad_spans,
)
from packages.planning.material.broll_pack import (
    _MIN_CLEAN_SPAN_SEC,
    _clip_id_for_clean_span,
    _diversity_key,
    _scene_from_clip,
)
from packages.planning.selection.recency import recency_penalty_for
from packages.planning.selection.recency_context import (
    build_portrait_recency_context_from_ledger,
)
from packages.production.pipeline._node_context import NodeContext

_BROLL_RECENT_SELECTION_LIMIT = 80
_PORTRAIT_MIN_CLEAN_SPAN_SEC = 0.08
_VISUAL_ELIGIBILITY_SCORE = 1.0
_VISUAL_ELIGIBILITY_POLICY_VERSION = "visual_hard_eligibility_v1"
_ELIGIBILITY_CACHE_SCHEMA_VERSION = "material_pack_eligibility_cache_v1"
_ELIGIBILITY_CACHE_MAX_ENTRIES = 32
_ELIGIBILITY_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()


def run(ctx: NodeContext) -> NodeOutput:
    request = ctx.state.request
    repo = ctx.repository
    assets = list(repo.media_assets.values())

    # Unified visual planning: every visual asset is split per clip into A-roll
    # (lip-sync-usable) vs B-roll (cover/backup) through one hard eligibility path.
    visual_assets = [asset for asset in assets if _eligible_visual_asset(asset, request)]
    broll_scoped_visual_assets = [
        asset
        for asset in visual_assets
        if request.broll.case_id is None or asset.case_id == request.broll.case_id
    ]
    eligible_bgm_assets = [asset for asset in assets if _eligible_asset(asset, "bgm", request)]
    eligible_font_assets = [asset for asset in assets if _eligible_asset(asset, "font", request)]

    stable, cache_hit, cache_key = _stable_eligibility_snapshot(
        ctx,
        assets=assets,
        visual_assets=visual_assets,
        broll_scoped_visual_assets=broll_scoped_visual_assets,
        eligible_bgm_assets=eligible_bgm_assets,
        eligible_font_assets=eligible_font_assets,
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

    portrait_reservation_rejections = _reserved_asset_rejections(
        visual_assets,
        reserved_asset_ids=portrait_reserved,
        medium="portrait",
    )
    broll_reservation_rejections = _reserved_asset_rejections(
        broll_scoped_visual_assets,
        reserved_asset_ids=broll_reserved,
        medium="broll",
    )
    bgm_reservation_rejections = _reserved_asset_rejections(
        eligible_bgm_assets,
        reserved_asset_ids=bgm_reserved,
        medium="bgm",
    )
    font_reservation_rejections = _reserved_asset_rejections(
        eligible_font_assets,
        reserved_asset_ids=font_reserved,
        medium="font",
    )

    portrait_ledger = repo.recent_selections(case_id=request.case_id, medium="portrait")
    portrait_candidates = _apply_portrait_recency(
        _candidate_models(stable["portrait_candidates"], reserved_asset_ids=portrait_reserved),
        ledger_entries=portrait_ledger,
    )
    portrait_rejections = [
        *portrait_reservation_rejections,
        *_filter_rejections(stable["portrait_rejections"], reserved_asset_ids=portrait_reserved),
    ]
    _portrait_from_video_count = sum(
        1 for c in portrait_candidates if (c.metadata or {}).get("clip_id")
    )

    broll_ledger = repo.recent_selections(
        case_id=request.case_id,
        medium="broll",
        limit=_BROLL_RECENT_SELECTION_LIMIT,
    )
    broll_candidates = _apply_broll_recency(
        _candidate_models(stable["broll_candidates"], reserved_asset_ids=broll_reserved),
        ledger_entries=broll_ledger,
    )
    broll_rejections = [
        *broll_reservation_rejections,
        *_filter_rejections(stable["broll_rejections"], reserved_asset_ids=broll_reserved),
    ]

    bgm_ledger = repo.recent_selections(case_id=request.case_id, medium="bgm")
    font_ledger = repo.recent_selections(case_id=request.case_id, medium="font")
    bgm_candidates = _apply_simple_recency(
        _candidate_models(stable["bgm_candidates"], reserved_asset_ids=bgm_reserved),
        ledger_entries=bgm_ledger,
        use_clip_id=True,
    )
    font_candidates = _apply_simple_recency(
        _candidate_models(stable["font_candidates"], reserved_asset_ids=font_reserved),
        ledger_entries=font_ledger,
        use_clip_id=False,
    )
    bgm_rejections = [
        *bgm_reservation_rejections,
        *_filter_rejections(stable["bgm_rejections"], reserved_asset_ids=bgm_reserved),
    ]
    font_rejections = [*font_reservation_rejections]

    portrait_visual_asset_ids = {
        asset_id for asset_id in stable["visual_asset_ids"] if asset_id not in portrait_reserved
    }
    broll_visual_asset_ids = {
        asset_id for asset_id in stable["broll_visual_asset_ids"] if asset_id not in broll_reserved
    }
    broll_annotation_asset_ids = {
        asset_id
        for asset_id in stable["broll_annotation_asset_ids"]
        if asset_id not in broll_reserved
    }
    broll_person_excluded = _sum_counts_excluding(
        stable["broll_person_excluded_by_asset"],
        reserved_asset_ids=broll_reserved,
    )
    broll_motion_excluded = _sum_counts_excluding(
        stable["broll_motion_excluded_by_asset"],
        reserved_asset_ids=broll_reserved,
    )
    portrait_motion_excluded = _sum_counts_excluding(
        stable["portrait_motion_excluded_by_asset"],
        reserved_asset_ids=portrait_reserved,
    )

    # §6.6 reserve: claim a TTL lease over every eligible asset that downstream
    # planners may choose so a concurrent same-case run does not silently collide.
    # The per-medium production node commits the asset it actually ships;
    # cancel/failure releases the rest. Assets a live run already holds were
    # filtered before eligibility; the reservation ids surfaced here are the ones
    # THIS run owns, wiring the previously-stubbed ``reservations`` contract field
    # for real.
    reservation_ids = _reserve_candidate_assets(
        ctx,
        case_id=request.case_id,
        portrait_candidates=portrait_candidates,
        broll_candidates=broll_candidates,
        bgm_candidates=bgm_candidates,
        font_candidates=font_candidates,
    )

    rejected_candidates = [
        *portrait_rejections,
        *broll_rejections,
        *bgm_rejections,
        *font_rejections,
    ]
    payload = MaterialPackArtifact(
        case_id=request.case_id,
        portrait_candidates=portrait_candidates,
        broll_candidates=broll_candidates,
        bgm_candidates=bgm_candidates,
        font_candidates=font_candidates,
        rejected_candidates=rejected_candidates,
        diagnostics={
            "visual_eligibility_policy_version": _VISUAL_ELIGIBILITY_POLICY_VERSION,
            "visual_eligibility_mode": "hard_gate_complete_pool",
            "materialpack_eligibility_cache": {
                "mode": "process_memory_lru",
                "schema_version": _ELIGIBILITY_CACHE_SCHEMA_VERSION,
                "hit": cache_hit,
                "key": cache_key,
                "entries": len(_ELIGIBILITY_CACHE),
            },
            "materialpack_ranking_disabled": {
                "portrait": True,
                "broll": True,
                "bgm": True,
                "font": True,
            },
            "visual_ranking_disabled": {"portrait": True, "broll": True},
            "portrait_missing": not portrait_candidates,
            "broll_missing": request.broll.enabled and not broll_candidates,
            "broll_unannotated": request.broll.enabled
            and bool(broll_visual_asset_ids)
            and not broll_annotation_asset_ids,
            "portrait_rejected": len(portrait_rejections),
            "broll_rejected": len(broll_rejections),
            "rejected_candidates": rejected_candidates,
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
            # early warning; TimelineWindowPlanning still enforces the hard coverage gate
            # downstream). Key names stay stable for downstream consumers.
            "portrait_from_video": _portrait_from_video_count,
            "video_no_lipsync": bool(portrait_visual_asset_ids)
            and _portrait_from_video_count == 0,
        },
        reservations=reservation_ids,
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[
            ctx.artifact(ArtifactKind.plan_material_pack, payload, "MaterialPackPlanArtifact.v1")
        ]
    )


def _is_ai_reference(asset) -> bool:
    # AI素材 (Seedance reference uploads) are case-scoped media assets tagged
    # ai_material. They must NEVER enter the digital-human / b-roll material
    # pools — they are reference inputs for generation, not footage to cut in.
    return "ai_material" in (getattr(asset, "tags", None) or [])


def _eligible_asset(asset, kind: str, request) -> bool:
    return (
        asset.usable
        and asset.kind == kind
        and asset.case_id in {None, request.case_id}
        and not _is_ai_reference(asset)
    )


def _eligible_visual_asset(asset, request) -> bool:
    # Visual assets are one unified ``video`` bucket (#99/#129/#133); A-roll vs
    # B-roll is a per-clip AnnotationV4 decision, not an ``asset.kind`` split. The
    # legacy ``portrait``/``broll`` kinds no longer exist (migration 0026/0033).
    return (
        asset.usable
        and asset.kind == "video"
        and asset.case_id in {None, request.case_id}
        and not _is_ai_reference(asset)
    )


def _stable_eligibility_snapshot(
    ctx: NodeContext,
    *,
    assets,
    visual_assets,
    broll_scoped_visual_assets,
    eligible_bgm_assets,
    eligible_font_assets,
) -> tuple[dict[str, Any], bool, str]:
    cache_key = _eligibility_cache_key(
        ctx,
        assets=assets,
        request=ctx.state.request,
    )
    cached = _ELIGIBILITY_CACHE.get(cache_key)
    if cached is not None:
        _ELIGIBILITY_CACHE.move_to_end(cache_key)
        return deepcopy(cached), True, cache_key

    repo = ctx.repository
    portrait_annotations = {
        asset.id: annotation
        for asset in visual_assets
        if (annotation := repo.annotation_v4_for_asset(asset.id)) is not None
    }
    portrait_annotation_rejections = _missing_annotation_rejections(
        visual_assets,
        annotations=portrait_annotations,
        medium="portrait",
    )
    portrait_candidates, portrait_clip_rejections = _eligible_portrait_candidates(
        ctx,
        annotations=portrait_annotations,
        ledger_entries=(),
    )

    broll_annotations = {
        asset.id: annotation
        for asset in broll_scoped_visual_assets
        if (annotation := repo.annotation_v4_for_asset(asset.id)) is not None
    }
    broll_annotation_rejections = _missing_annotation_rejections(
        broll_scoped_visual_assets,
        annotations=broll_annotations,
        medium="broll",
    )
    broll_candidates, broll_clip_rejections = _eligible_broll_candidates(
        annotations=broll_annotations,
        ledger_entries=(),
    )

    bgm_annotations = {
        asset.id: annotation
        for asset in eligible_bgm_assets
        if (annotation := repo.annotation_v4_for_asset(asset.id)) is not None
    }
    bgm_annotation_rejections = _missing_annotation_rejections(
        eligible_bgm_assets,
        annotations=bgm_annotations,
        medium="bgm",
    )
    bgm_candidates, bgm_segment_rejections = _bgm_segment_candidates(
        eligible_bgm_assets,
        bgm_annotations,
        ledger_entries=(),
    )
    font_candidates = _simple_candidates(eligible_font_assets, "font", ledger_entries=())

    snapshot = {
        "portrait_candidates": _candidate_payloads(portrait_candidates),
        "broll_candidates": _candidate_payloads(broll_candidates),
        "bgm_candidates": _candidate_payloads(bgm_candidates),
        "font_candidates": _candidate_payloads(font_candidates),
        "portrait_rejections": [
            *portrait_annotation_rejections,
            *portrait_clip_rejections,
        ],
        "broll_rejections": [
            *broll_annotation_rejections,
            *broll_clip_rejections,
        ],
        "bgm_rejections": [
            *bgm_annotation_rejections,
            *bgm_segment_rejections,
        ],
        "visual_asset_ids": [asset.id for asset in visual_assets],
        "broll_visual_asset_ids": [asset.id for asset in broll_scoped_visual_assets],
        "broll_annotation_asset_ids": sorted(broll_annotations),
        "broll_person_excluded_by_asset": _broll_person_excluded_counts(broll_annotations),
        "broll_motion_excluded_by_asset": _broll_motion_excluded_counts(broll_annotations),
        "portrait_motion_excluded_by_asset": _portrait_motion_excluded_counts(
            portrait_annotations
        ),
    }
    _ELIGIBILITY_CACHE[cache_key] = deepcopy(snapshot)
    _ELIGIBILITY_CACHE.move_to_end(cache_key)
    while len(_ELIGIBILITY_CACHE) > _ELIGIBILITY_CACHE_MAX_ENTRIES:
        _ELIGIBILITY_CACHE.popitem(last=False)
    return deepcopy(snapshot), False, cache_key


def _eligibility_cache_key(ctx: NodeContext, *, assets, request) -> str:
    repo = ctx.repository
    scoped_assets = [
        asset
        for asset in assets
        if (
            _eligible_visual_asset(asset, request)
            or _eligible_asset(asset, "bgm", request)
            or _eligible_asset(asset, "font", request)
        )
    ]
    payload = {
        "schema_version": _ELIGIBILITY_CACHE_SCHEMA_VERSION,
        "case_id": request.case_id,
        "broll_case_id": request.broll.case_id,
        "visual_eligibility_policy_version": _VISUAL_ELIGIBILITY_POLICY_VERSION,
        "assets": [
            _asset_cache_signature(repo, asset)
            for asset in sorted(scoped_assets, key=lambda item: item.id)
        ],
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"mpelig_{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _asset_cache_signature(repo, asset) -> dict[str, Any]:
    annotation = repo.annotations.get(asset.id)
    source = repo.artifacts.get(asset.source_artifact_id) if asset.source_artifact_id else None
    media_info = getattr(source, "media_info", None)
    return {
        "id": asset.id,
        "case_id": asset.case_id,
        "kind": asset.kind,
        "source_artifact_id": asset.source_artifact_id,
        "duration_sec": asset.duration_sec,
        "source_duration_sec": getattr(media_info, "duration_sec", None),
        "tags": sorted(getattr(asset, "tags", None) or []),
        "usable": asset.usable,
        "version": asset.version,
        "schema_version": asset.schema_version,
        "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
        "annotation_etag": getattr(annotation, "etag", None),
        "annotation_hash": _annotation_hash(annotation),
    }


def _annotation_hash(annotation) -> str | None:
    if annotation is None:
        return None
    canonical = getattr(annotation, "canonical", None)
    if hasattr(canonical, "model_dump"):
        payload = canonical.model_dump(mode="json")
    elif isinstance(canonical, dict):
        payload = canonical
    else:
        payload = None
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _candidate_payloads(candidates: list[MaterialCandidate]) -> list[dict[str, Any]]:
    return [candidate.model_dump(mode="json") for candidate in candidates]


def _candidate_models(
    candidates: list[dict[str, Any]],
    *,
    reserved_asset_ids: set[str],
) -> list[MaterialCandidate]:
    return [
        MaterialCandidate.model_validate(candidate)
        for candidate in candidates
        if str(candidate.get("asset_id") or "") not in reserved_asset_ids
    ]


def _filter_rejections(
    rejections: list[dict[str, Any]],
    *,
    reserved_asset_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        dict(rejection)
        for rejection in rejections
        if str(rejection.get("asset_id") or "") not in reserved_asset_ids
    ]


def _apply_portrait_recency(
    candidates: list[MaterialCandidate],
    *,
    ledger_entries,
) -> list[MaterialCandidate]:
    recent_usage_cache: dict[str, dict] = {}
    updated: list[MaterialCandidate] = []
    for candidate in candidates:
        recent_usage = recent_usage_cache.get(candidate.asset_id)
        if recent_usage is None:
            recent_usage = build_portrait_recency_context_from_ledger(
                entries=ledger_entries,
                template_id=candidate.asset_id,
                diversity_key=None,
            )
            recent_usage_cache[candidate.asset_id] = recent_usage
        metadata = dict(candidate.metadata or {})
        metadata["recency_penalty"] = round(float(recent_usage.get("recency_penalty") or 0.0), 3)
        metadata["recent_usage"] = recent_usage
        updated.append(candidate.model_copy(update={"metadata": metadata}))
    return updated


def _apply_broll_recency(
    candidates: list[MaterialCandidate],
    *,
    ledger_entries,
) -> list[MaterialCandidate]:
    updated: list[MaterialCandidate] = []
    for candidate in candidates:
        metadata = dict(candidate.metadata or {})
        penalty = round(
            recency_penalty_for(
                ledger_entries,
                asset_id=candidate.asset_id,
                diversity_key=metadata.get("diversity_key"),
            ),
            3,
        )
        metadata["recency_penalty"] = penalty
        metadata["recent_usage"] = {
            "is_recently_used": penalty > 0.0,
            "recency_penalty": penalty,
        }
        updated.append(candidate.model_copy(update={"metadata": metadata}))
    return updated


def _apply_simple_recency(
    candidates: list[MaterialCandidate],
    *,
    ledger_entries,
    use_clip_id: bool,
) -> list[MaterialCandidate]:
    updated: list[MaterialCandidate] = []
    for candidate in candidates:
        metadata = dict(candidate.metadata or {})
        penalty = round(
            recency_penalty_for(
                ledger_entries,
                asset_id=candidate.asset_id,
                clip_id=str(metadata.get("clip_id") or "") if use_clip_id else None,
            ),
            3,
        )
        metadata["recency_penalty"] = penalty
        metadata["recent_usage"] = {
            "is_recently_used": penalty > 0.0,
            "recency_penalty": penalty,
        }
        updated.append(candidate.model_copy(update={"metadata": metadata}))
    return updated


def _sum_counts_excluding(counts_by_asset: dict[str, int], *, reserved_asset_ids: set[str]) -> int:
    return sum(
        int(count)
        for asset_id, count in counts_by_asset.items()
        if asset_id not in reserved_asset_ids
    )


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
    try:
        source = ctx.source_artifact_for_asset(asset_id)
    except NodeExecutionError:
        source = None
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


def _reserved_asset_rejections(assets, *, reserved_asset_ids: set[str], medium: str) -> list[dict]:
    return [
        _asset_rejection(medium=medium, asset_id=asset.id, reason="active_selection_reservation")
        for asset in assets
        if asset.id in reserved_asset_ids
    ]


def _missing_annotation_rejections(assets, *, annotations, medium: str) -> list[dict]:
    return [
        _asset_rejection(medium=medium, asset_id=asset.id, reason="annotation_missing")
        for asset in assets
        if asset.id not in annotations
    ]


def _eligible_portrait_candidates(
    ctx: NodeContext,
    *,
    annotations,
    ledger_entries,
) -> tuple[list[MaterialCandidate], list[dict]]:
    """Return every portrait source window that passes hard eligibility gates."""
    candidates: list[MaterialCandidate] = []
    rejections: list[dict] = []
    recent_usage_cache: dict[str, dict] = {}

    for asset_id in sorted(annotations):
        annotation = annotations[asset_id]
        bad_spans = avoid_intervals(annotation)
        source_duration = _source_duration_for_asset(
            ctx,
            asset_id=asset_id,
            annotation=annotation,
        )
        for clip in annotation.clips:
            clip_id = str(getattr(clip, "segment_id", "") or "")
            if not clip_is_lip_sync_usable(clip):
                rejections.append(
                    _candidate_rejection(
                        medium="portrait",
                        asset_id=asset_id,
                        clip=clip,
                        reason=_portrait_rejection_reason(clip),
                    )
                )
                continue
            clean_windows = clean_portrait_source_windows(
                {
                    "source_start": clip.start,
                    "source_end": clip.end,
                    "avoid_spans": bad_spans,
                },
                source_duration=source_duration,
            )
            if not clean_windows:
                rejections.append(
                    _candidate_rejection(
                        medium="portrait",
                        asset_id=asset_id,
                        clip=clip,
                        reason="portrait_no_clean_source_window",
                    )
                )
                continue
            recent_usage = recent_usage_cache.get(asset_id)
            if recent_usage is None:
                recent_usage = build_portrait_recency_context_from_ledger(
                    entries=ledger_entries,
                    template_id=asset_id,
                    diversity_key=None,
                )
                recent_usage_cache[asset_id] = recent_usage
            for clean_index, (clean_start, clean_end) in enumerate(clean_windows):
                source_window_id = clip_id if clean_index == 0 else f"{clip_id}:m{clean_index}"
                clean_duration = round(clean_end - clean_start, 3)
                candidates.append(
                    MaterialCandidate(
                        asset_id=asset_id,
                        score=_VISUAL_ELIGIBILITY_SCORE,
                        reason="eligible portrait clip",
                        metadata={
                            "eligibility": "passed_hard_gate",
                            "eligibility_policy_version": _VISUAL_ELIGIBILITY_POLICY_VERSION,
                            "clip_id": clip_id,
                            "source_window_id": source_window_id,
                            "source_start": round(clean_start, 3),
                            "source_end": round(clean_end, 3),
                            "source_frames_available": _source_frames_available(
                                clean_start,
                                clean_end,
                            ),
                            "duration": clean_duration,
                            "lip_sync_confidence": float(getattr(clip, "confidence", 0.0) or 0.0),
                            "recency_penalty": round(
                                float(recent_usage.get("recency_penalty") or 0.0),
                                3,
                            ),
                            "recent_usage": recent_usage,
                        },
                    )
                )
    candidates.sort(
        key=lambda c: (
            c.asset_id,
            str((c.metadata or {}).get("clip_id") or ""),
            float((c.metadata or {}).get("source_start") or 0.0),
        )
    )
    return candidates, rejections


def _eligible_broll_candidates(
    *,
    annotations,
    ledger_entries,
) -> tuple[list[MaterialCandidate], list[dict]]:
    """Return every clean B-roll clip span that passes hard eligibility gates."""
    candidates: list[MaterialCandidate] = []
    rejections: list[dict] = []

    for asset_id in sorted(annotations):
        annotation = annotations[asset_id]
        bad_spans = avoid_intervals(annotation)
        for clip in annotation.clips:
            if _clip_role(clip) == "avoid":
                rejections.append(
                    _candidate_rejection(
                        medium="broll",
                        asset_id=asset_id,
                        clip=clip,
                        reason="broll_role_avoid",
                    )
                )
                continue
            if clip_is_lip_sync_usable(clip):
                rejections.append(
                    _candidate_rejection(
                        medium="broll",
                        asset_id=asset_id,
                        clip=clip,
                        reason="broll_lipsync_routed_to_portrait",
                    )
                )
                continue
            if clip_shows_person(clip):
                rejections.append(
                    _candidate_rejection(
                        medium="broll",
                        asset_id=asset_id,
                        clip=clip,
                        reason="broll_person_clip",
                    )
                )
                continue
            clean_spans = subtract_bad_spans(
                clip.start,
                clip.end,
                bad_spans,
                min_len=_MIN_CLEAN_SPAN_SEC,
            )
            if not clean_spans:
                rejections.append(
                    _candidate_rejection(
                        medium="broll",
                        asset_id=asset_id,
                        clip=clip,
                        reason="broll_no_clean_source_span",
                    )
                )
                continue
            for span_index, clean_span in enumerate(clean_spans):
                scene = _scene_from_clip(clip, span=clean_span)
                candidate_clip_id = _clip_id_for_clean_span(clip.segment_id, span_index)
                diversity_key = _diversity_key(clip)
                penalty = round(
                    recency_penalty_for(
                        ledger_entries,
                        asset_id=asset_id,
                        diversity_key=diversity_key,
                    ),
                    3,
                )
                source_start = round(float(clean_span[0]), 3)
                source_end = round(float(clean_span[1]), 3)
                candidates.append(
                    MaterialCandidate(
                        asset_id=asset_id,
                        score=_VISUAL_ELIGIBILITY_SCORE,
                        reason="eligible b-roll clip",
                        metadata={
                            "eligibility": "passed_hard_gate",
                            "eligibility_policy_version": _VISUAL_ELIGIBILITY_POLICY_VERSION,
                            "clip_id": candidate_clip_id,
                            "matched_keywords": list(scene.keywords),
                            "scene_name": scene.name,
                            "source_start": source_start,
                            "source_end": source_end,
                            "source_frames_available": _source_frames_available(
                                source_start,
                                source_end,
                            ),
                            "recency_penalty": penalty,
                            "recent_usage": {
                                "is_recently_used": penalty > 0.0,
                                "recency_penalty": penalty,
                            },
                            "diversity_key": diversity_key,
                        },
                    )
                )
    candidates.sort(
        key=lambda c: (
            c.asset_id,
            str((c.metadata or {}).get("clip_id") or ""),
            float((c.metadata or {}).get("source_start") or 0.0),
        )
    )
    return candidates, rejections


def _candidate_rejection(*, medium: str, asset_id: str, clip, reason: str) -> dict:
    payload = {
        "medium": medium,
        "asset_id": asset_id,
        "clip_id": str(getattr(clip, "segment_id", "") or ""),
        "reason": reason,
        "source_start": round(float(getattr(clip, "start", 0.0) or 0.0), 3),
        "source_end": round(float(getattr(clip, "end", 0.0) or 0.0), 3),
    }
    return payload


def _asset_rejection(*, medium: str, asset_id: str, reason: str) -> dict:
    return {
        "medium": medium,
        "asset_id": asset_id,
        "clip_id": None,
        "reason": reason,
    }


def _portrait_rejection_reason(clip) -> str:
    if _clip_role(clip) == "avoid":
        return "portrait_role_avoid"
    usage = getattr(clip, "usage", None)
    if bool(getattr(usage, "voiceover_only", False)):
        return "portrait_voiceover_only"
    fcm = getattr(getattr(clip, "semantics", None), "face_count_max", None)
    if fcm is not None and fcm > 1:
        return "portrait_multiple_faces"
    try:
        if float(getattr(clip, "end", 0.0)) - float(getattr(clip, "start", 0.0)) < 0.6:
            return "portrait_too_short"
    except (TypeError, ValueError):
        return "portrait_invalid_source_span"
    return "portrait_not_lipsync_usable"


def _clip_role(clip) -> str:
    role = getattr(getattr(clip, "usage", None), "role", "")
    return str(role.value if hasattr(role, "value") else role)


def _source_frames_available(source_start: float, source_end: float) -> int:
    return max(0, frame_index(source_end) - frame_index(source_start))


def _broll_person_excluded_counts(annotations) -> dict[str, int]:
    excluded: dict[str, int] = {}
    for asset_id, annotation in annotations.items():
        count = 0
        for clip in annotation.clips:
            if (
                clip.usage.role.value != "avoid"
                and not clip_is_lip_sync_usable(clip)
                and clip_shows_person(clip)
            ):
                count += 1
        if count:
            excluded[asset_id] = count
    return excluded


def _broll_motion_excluded_counts(annotations) -> dict[str, int]:
    excluded: dict[str, int] = {}
    for asset_id, annotation in annotations.items():
        count = 0
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
                count += 1
        if count:
            excluded[asset_id] = count
    return excluded


def _broll_motion_excluded_count(annotations) -> int:
    return sum(_broll_motion_excluded_counts(annotations).values())


def _portrait_motion_excluded_counts(annotations) -> dict[str, int]:
    excluded: dict[str, int] = {}
    for asset_id, annotation in annotations.items():
        count = 0
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
                count += 1
        if count:
            excluded[asset_id] = count
    return excluded


def _portrait_motion_excluded_count(annotations) -> int:
    return sum(_portrait_motion_excluded_counts(annotations).values())


def _clip_overlaps_bad_span(clip, bad_spans: list[tuple[float, float]]) -> bool:
    start = round(float(clip.start), 3)
    end = round(float(clip.end), 3)
    return any(min(end, bad_end) > max(start, bad_start) for bad_start, bad_end in bad_spans)


def _reserve_candidate_assets(
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
        asset_ids = list(dict.fromkeys(c.asset_id for c in candidates if c.asset_id))
        if not asset_ids:
            continue
        diversity_keys = {
            c.asset_id: (c.metadata or {}).get("diversity_key")
            for c in candidates
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
        penalty = round(recency_penalty_for(ledger_entries, asset_id=asset.id), 3)
        candidates.append(
            MaterialCandidate(
                asset_id=asset.id,
                score=_VISUAL_ELIGIBILITY_SCORE,
                reason=f"eligible {medium_label}",
                metadata={
                    "eligibility": "passed_hard_gate",
                    "eligibility_policy_version": _VISUAL_ELIGIBILITY_POLICY_VERSION,
                    "recency_penalty": penalty,
                    "recent_usage": {
                        "is_recently_used": penalty > 0.0,
                        "recency_penalty": penalty,
                    },
                },
            )
        )
    candidates.sort(key=lambda c: c.asset_id)
    return candidates


def _bgm_segment_candidates(
    assets,
    annotations,
    ledger_entries,
) -> tuple[list[MaterialCandidate], list[dict]]:
    candidates: list[MaterialCandidate] = []
    rejections: list[dict] = []
    for asset in assets:
        annotation = annotations.get(asset.id)
        if annotation is None:
            continue
        if not annotation.bgm_segments:
            rejections.append(
                _asset_rejection(medium="bgm", asset_id=asset.id, reason="bgm_no_segments")
            )
            continue
        for segment in annotation.bgm_segments:
            segment_id = str(segment.segment_id or "").strip()
            if not segment_id:
                rejections.append(
                    _bgm_segment_rejection(
                        asset_id=asset.id,
                        segment=segment,
                        reason="bgm_segment_missing_id",
                    )
                )
                continue
            source_start = round(float(segment.start), 3)
            source_end = round(float(segment.end), 3)
            duration = round(max(0.0, source_end - source_start), 3)
            if duration <= 0:
                rejections.append(
                    _bgm_segment_rejection(
                        asset_id=asset.id,
                        segment=segment,
                        reason="bgm_segment_invalid_duration",
                    )
                )
                continue
            penalty = recency_penalty_for(
                ledger_entries,
                asset_id=asset.id,
                clip_id=segment_id,
            )
            role = segment.role.value if hasattr(segment.role, "value") else str(segment.role)
            candidates.append(
                MaterialCandidate(
                    asset_id=asset.id,
                    score=_VISUAL_ELIGIBILITY_SCORE,
                    reason="eligible BGM segment",
                    metadata={
                        "eligibility": "passed_hard_gate",
                        "eligibility_policy_version": _VISUAL_ELIGIBILITY_POLICY_VERSION,
                        "recency_penalty": round(penalty, 3),
                        "recent_usage": {
                            "is_recently_used": penalty > 0.0,
                            "recency_penalty": round(penalty, 3),
                        },
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
            c.asset_id,
            float((c.metadata or {}).get("source_start") or 0.0),
            str((c.metadata or {}).get("clip_id") or ""),
        )
    )
    return candidates, rejections


def _bgm_segment_rejection(*, asset_id: str, segment, reason: str) -> dict:
    return {
        "medium": "bgm",
        "asset_id": asset_id,
        "clip_id": str(getattr(segment, "segment_id", "") or ""),
        "reason": reason,
        "source_start": round(float(getattr(segment, "start", 0.0) or 0.0), 3),
        "source_end": round(float(getattr(segment, "end", 0.0) or 0.0), 3),
    }
