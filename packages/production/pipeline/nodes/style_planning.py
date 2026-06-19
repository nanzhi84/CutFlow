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
    bgm_candidates = [
        item
        for item in material.get("bgm_candidates", [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    font_candidates = [
        item.get("asset_id") for item in material.get("font_candidates", []) if item.get("asset_id")
    ]
    degradations: list[DegradationNotice] = []
    warnings: list[WarningCode] = []
    selected_bgm = (
        _select_bgm_candidate(bgm_candidates, requested_asset_id=state.request.bgm.bgm_id)
        if state.request.bgm.enabled
        else None
    )
    bgm_asset_id = selected_bgm.get("asset_id") if selected_bgm else None
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
    bgm_metadata = selected_bgm.get("metadata") if isinstance(selected_bgm, dict) else {}
    if not isinstance(bgm_metadata, dict):
        bgm_metadata = {}
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
                segment_id=_str_or_none(bgm_metadata.get("clip_id")),
                source_start=_float_or_none(bgm_metadata.get("source_start")),
                source_end=_float_or_none(bgm_metadata.get("source_end")),
                duration=_float_or_none(bgm_metadata.get("duration")),
                mood=str(bgm_metadata.get("mood") or ""),
                scene_fit=[
                    str(item)
                    for item in (bgm_metadata.get("scene_fit") or [])
                    if isinstance(item, str) and item
                ],
                reason=str(bgm_metadata.get("reason") or selected_bgm.get("reason") or "")
                if selected_bgm
                else "",
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


def _select_bgm_candidate(candidates: list[dict], *, requested_asset_id: str | None) -> dict | None:
    if requested_asset_id:
        return next(
            (
                candidate
                for candidate in candidates
                if candidate.get("asset_id") == requested_asset_id
            ),
            None,
        )
    return candidates[0] if candidates else None


def _str_or_none(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
