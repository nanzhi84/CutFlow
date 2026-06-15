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
from packages.planning.material import (
    extract_keywords,
    rank_broll_candidates,
    score_portrait_candidate,
    score_simple_candidate,
    segment_script,
)
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    request = ctx.state.request
    repo = ctx.repository
    assets = list(repo.media_assets.values())

    def _eligible(asset, kind: str) -> bool:
        return (
            asset.usable
            and asset.kind == kind
            and asset.case_id in {None, request.case_id}
        )

    portrait_assets = [
        asset
        for asset in assets
        if _eligible(asset, "portrait")
        and (
            request.portrait.template_mode == "agent"
            or asset.id == request.portrait.specific_template_id
            or asset.id in request.portrait.template_sequence_ids
        )
    ]
    broll_assets = [
        asset
        for asset in assets
        if _eligible(asset, "broll")
        and (request.broll.case_id is None or asset.case_id == request.broll.case_id)
    ]
    bgm_assets = [asset for asset in assets if _eligible(asset, "bgm")]
    font_assets = [asset for asset in assets if _eligible(asset, "font")]

    # --- portrait (coverage is enforced later; here: lip-sync + recency) ------
    portrait_ledger = repo.recent_selections(case_id=request.case_id, medium="portrait")
    portrait_candidates: list[MaterialCandidate] = []
    for asset in portrait_assets:
        source = ctx.source_artifact_for_asset(asset.id)
        source_duration = (
            float(source.media_info.duration_sec or 0)
            if source and source.media_info
            else 0.0
        )
        annotation = repo.annotation_v4_for_asset(asset.id)
        scored = score_portrait_candidate(
            asset_id=asset.id,
            source_duration=source_duration,
            required_duration=source_duration,  # coverage gate lives in PortraitPlanning
            annotation=annotation,
            ledger_entries=portrait_ledger,
        )
        portrait_candidates.append(
            MaterialCandidate(
                asset_id=asset.id,
                score=scored.score,
                reason=scored.reason,
                metadata={"base_score": scored.base_score, "recency_penalty": scored.recency_penalty},
            )
        )
    portrait_candidates.sort(key=lambda c: (-c.score, c.asset_id))

    # --- b-roll (real annotation matching; no annotation -> no candidate) -----
    keywords = extract_keywords(request.script)
    segments = segment_script(request.script, keywords=keywords)
    broll_ledger = repo.recent_selections(case_id=request.case_id, medium="broll")
    broll_annotations = {
        asset.id: annotation
        for asset in broll_assets
        if (annotation := repo.annotation_v4_for_asset(asset.id)) is not None
    }
    broll_candidates: list[MaterialCandidate] = []
    for candidate in rank_broll_candidates(
        annotations=broll_annotations,
        segments=segments,
        ledger_entries=broll_ledger,
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
                },
            )
        )

    # --- bgm / font (availability + recency) ---------------------------------
    bgm_ledger = repo.recent_selections(case_id=request.case_id, medium="bgm")
    font_ledger = repo.recent_selections(case_id=request.case_id, medium="font")
    bgm_candidates = _simple_candidates(bgm_assets, "bgm", bgm_ledger)
    font_candidates = _simple_candidates(font_assets, "font", font_ledger)

    # §6.6 reserve: claim a TTL lease over each top candidate per medium so a
    # concurrent same-case run does not silently collide on the same asset. The
    # per-medium production node commits the asset it actually ships; cancel/failure
    # releases the rest. Assets a live run already holds are skipped (recency already
    # demoted them upstream); the reservation ids surfaced here are the ones THIS run
    # owns, wiring the previously-stubbed ``reservations`` contract field for real.
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
            and bool(broll_assets)
            and not broll_annotations,
            "bgm_missing": request.bgm.enabled and not bgm_candidates,
        },
        reservations=reservation_ids,
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[ctx.artifact(ArtifactKind.plan_material_pack, payload, "MaterialPackPlanArtifact.v1")]
    )


# How many top-ranked candidates to reserve per medium. Reserving the shortlist (not
# only the single eventual pick) is intentional: the production node may pick any of the
# top candidates, and uncommitted reservations are released at finalize/failure.
_RESERVE_TOP_N = 3


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
        asset_ids = [c.asset_id for c in candidates[:_RESERVE_TOP_N] if c.asset_id]
        if not asset_ids:
            continue
        diversity_keys = {
            c.asset_id: (c.metadata or {}).get("diversity_key")
            for c in candidates[:_RESERVE_TOP_N]
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
                metadata={"base_score": scored.base_score, "recency_penalty": scored.recency_penalty},
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.asset_id))
    return candidates
