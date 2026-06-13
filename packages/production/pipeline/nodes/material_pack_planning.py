"""MaterialPackPlanning node: gather usable portrait/b-roll/bgm/font candidates."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind
from packages.core.contracts.artifacts import MaterialCandidate, MaterialPackArtifact
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._selection import candidate_metadata


def run(ctx: NodeContext) -> NodeOutput:
    request = ctx.state.request
    assets = list(ctx.repository.media_assets.values())
    asset_by_id = {asset.id: asset for asset in assets}
    portrait = [
        asset.id
        for asset in assets
        if asset.usable
        and asset.kind == "portrait"
        and (asset.case_id in {None, request.case_id})
        and (
            request.portrait.template_mode == "agent"
            or asset.id == request.portrait.specific_template_id
            or asset.id in request.portrait.template_sequence_ids
        )
    ]
    broll = [
        asset.id
        for asset in assets
        if asset.usable
        and asset.kind == "broll"
        and (asset.case_id in {None, request.case_id})
        and (request.broll.case_id is None or asset.case_id == request.broll.case_id)
    ]
    bgm = [
        asset.id
        for asset in assets
        if asset.usable and asset.kind == "bgm" and asset.case_id in {None, request.case_id}
    ]
    fonts = [
        asset.id
        for asset in assets
        if asset.usable and asset.kind == "font" and asset.case_id in {None, request.case_id}
    ]
    payload = MaterialPackArtifact(
        case_id=request.case_id,
        portrait_candidates=[
            MaterialCandidate(
                asset_id=asset_id,
                score=1,
                reason="seeded usable portrait",
                metadata=candidate_metadata(asset_by_id.get(asset_id)),
            )
            for asset_id in portrait
        ],
        broll_candidates=[
            MaterialCandidate(
                asset_id=asset_id,
                score=1,
                reason="seeded usable b-roll",
                metadata=candidate_metadata(asset_by_id.get(asset_id)),
            )
            for asset_id in broll
        ],
        bgm_candidates=[
            MaterialCandidate(
                asset_id=asset_id,
                score=1,
                reason="seeded usable bgm",
                metadata=candidate_metadata(asset_by_id.get(asset_id)),
            )
            for asset_id in bgm
        ],
        font_candidates=[
            MaterialCandidate(
                asset_id=asset_id,
                score=1,
                reason="seeded usable font",
                metadata=candidate_metadata(asset_by_id.get(asset_id)),
            )
            for asset_id in fonts
        ],
        diagnostics={
            "portrait_missing": not bool(portrait),
            "broll_missing": request.broll.enabled and not bool(broll),
            "bgm_missing": request.bgm.enabled and not bool(bgm),
        },
        reservations=[new_id("reserve")],
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[ctx.artifact(ArtifactKind.plan_material_pack, payload, "MaterialPackPlanArtifact.v1")]
    )
