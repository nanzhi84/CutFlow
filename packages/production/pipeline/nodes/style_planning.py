"""StylePlanning node: subtitle/BGM/font style plan with degradations."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, DegradationNotice, NodeStatus, WarningCode
from packages.core.contracts.artifacts import (
    BgmPlan,
    FontPlan,
    StylePlanArtifact,
    SubtitleStylePlan,
)
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    node_run = ctx.node_run
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    bgm_candidates = [item.get("asset_id") for item in material.get("bgm_candidates", []) if item.get("asset_id")]
    font_candidates = [item.get("asset_id") for item in material.get("font_candidates", []) if item.get("asset_id")]
    degradations: list[DegradationNotice] = []
    warnings: list[WarningCode] = []
    bgm_asset_id = state.request.bgm.bgm_id or (bgm_candidates[0] if bgm_candidates else None)
    if state.request.bgm.enabled and not bgm_asset_id:
        degradations.append(
            degradation_notice(
                WarningCode.bgm_skipped_library_unannotated,
                "BGM library is not annotated.",
                node_id=node_run.node_id,
                affects_true_yield=False,
            )
        )
        warnings.append(WarningCode.bgm_skipped_library_unannotated)
    font_asset_id = font_candidates[0] if font_candidates else "case_default_font"
    if not font_candidates:
        warnings.append(WarningCode.font_default_used)
    artifact = ctx.artifact(
        ArtifactKind.plan_style,
        StylePlanArtifact(
            subtitle=SubtitleStylePlan(
                enabled=state.request.subtitle.enabled,
                style_preset=state.request.subtitle.style_preset,
                font_id=state.request.subtitle.font_id,
                font_size=state.request.subtitle.font_size,
                position=state.request.subtitle.position,
            ),
            bgm=BgmPlan(
                enabled=state.request.bgm.enabled,
                asset_id=bgm_asset_id,
                volume=state.request.bgm.volume,
                auto_mix=state.request.bgm.auto_mix,
            ),
            font=FontPlan(font_id=font_asset_id, size=state.request.subtitle.font_size),
            font_asset_id=font_asset_id,
            bgm_asset_id=bgm_asset_id,
            subtitle_enabled=state.request.subtitle.enabled,
        ).model_dump(mode="json"),
        "StylePlanArtifact.v1",
    )
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[artifact],
        warnings=warnings,
        degradations=degradations,
    )
