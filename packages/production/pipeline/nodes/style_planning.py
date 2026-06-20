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
        _select_bgm_candidate(
            bgm_candidates,
            requested_asset_id=state.request.bgm.bgm_id,
            script=state.request.script,
        )
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
                section_type=str(bgm_metadata.get("section_type") or ""),
                section_label=str(bgm_metadata.get("section_label") or ""),
                repeat_group=str(bgm_metadata.get("repeat_group") or ""),
                loopable=_bool_from_metadata(bgm_metadata.get("loopable")),
                energy_profile=str(bgm_metadata.get("energy_profile") or ""),
                mood=str(bgm_metadata.get("mood") or ""),
                scene_fit=_string_list(bgm_metadata.get("scene_fit")),
                script_fit=_string_list(bgm_metadata.get("script_fit")),
                avoid_script=_string_list(bgm_metadata.get("avoid_script")),
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


def _select_bgm_candidate(
    candidates: list[dict],
    *,
    requested_asset_id: str | None,
    script: str,
) -> dict | None:
    if requested_asset_id:
        candidates = [
            candidate for candidate in candidates if candidate.get("asset_id") == requested_asset_id
        ]
    if not candidates:
        return None
    ranked = [
        (
            _bgm_script_choice_score(candidate, script=script),
            -index,
            candidate,
        )
        for index, candidate in enumerate(candidates)
    ]
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked[0][2]


def _bgm_script_choice_score(candidate: dict, *, script: str) -> float:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    base = _float_or_none(candidate.get("score")) or 0.0
    positive = _match_count(
        script,
        [
            *(_string_list(metadata.get("script_fit"))),
            *(_string_list(metadata.get("scene_fit"))),
            str(metadata.get("reason") or ""),
            str(candidate.get("reason") or ""),
            str(metadata.get("mood") or ""),
        ],
    )
    negative = _match_count(script, _string_list(metadata.get("avoid_script")))
    return base + positive * 50.0 - negative * 80.0


def _match_count(script: str, labels: list[str]) -> int:
    haystack = _compact_text(script)
    if not haystack:
        return 0
    count = 0
    for label in labels:
        needle = _compact_text(label)
        if len(needle) >= 2 and needle in haystack:
            count += 1
    return count


def _compact_text(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if not ch.isspace())


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _bool_from_metadata(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _str_or_none(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
