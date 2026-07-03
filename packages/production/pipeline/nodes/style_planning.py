"""StylePlanning node: subtitle/BGM/font style plan with degradations."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, DegradationNotice, NodeStatus, WarningCode
from packages.core.contracts.artifacts import (
    BgmPlan,
    EmphasisHint,
    FontPlan,
    OverlayEvent,
    StylePlanArtifact,
    SubtitleStylePlan,
)
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline.nodes._creative_intent import load_creative_intent

_SUBTITLE_PRESET_DEFAULTS = {
    "douyin": {"font_size": 64, "position": {"x": 0.5, "y": 0.88}},
    "clean": {"font_size": 52, "position": {"x": 0.5, "y": 0.86}},
    "variety": {"font_size": 68, "position": {"x": 0.5, "y": 0.84}},
    "news": {"font_size": 48, "position": {"x": 0.5, "y": 0.90}},
    "movie": {"font_size": 44, "position": {"x": 0.5, "y": 0.82}},
    "youshe_title_black": {"font_size": 66, "position": {"x": 0.5, "y": 0.86}},
}


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    node_run = ctx.node_run
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    raw_bgm_candidates = [
        item
        for item in material.get("bgm_candidates", [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    bgm_candidates = [
        item for item in raw_bgm_candidates if _is_segmented_bgm_candidate(item)
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
    # Emphasis 花字地基：把 LLM 标记的关键短语确定性地落到含它的旁白句上，换算成带时间轴的
    # OverlayEvent（渲染层叠成独立样式字幕）。narration_units 在本节点之前已产出。
    narration_units = state.artifacts.get(ArtifactKind.narration_units)
    units = (narration_units.payload or {}).get("units", []) if narration_units is not None else []
    overlay_events = _derive_overlay_events(load_creative_intent(state).emphasis, units)
    artifact = ctx.artifact(
        ArtifactKind.plan_style,
        StylePlanArtifact(
            subtitle=SubtitleStylePlan(
                font_id=state.request.subtitle.font_id,
                font_size=_subtitle_font_size(state.request.subtitle.style_preset, state.request.subtitle.font_size),
                position=_subtitle_position(state.request.subtitle.style_preset, state.request.subtitle.position),
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
            font=FontPlan(font_id=font_asset_id),
            font_asset_id=font_asset_id,
            bgm_asset_id=bgm_asset_id,
            overlay_events=overlay_events,
        ).model_dump(mode="json"),
        "StylePlanArtifact.v1",
    )
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[artifact],
        warnings=warnings,
        degradations=degradations,
    )


def _subtitle_preset(style_preset: str) -> dict:
    return _SUBTITLE_PRESET_DEFAULTS.get(style_preset, _SUBTITLE_PRESET_DEFAULTS["douyin"])


def _subtitle_font_size(style_preset: str, explicit_size: int | None) -> int:
    if explicit_size is not None:
        return explicit_size
    return int(_subtitle_preset(style_preset)["font_size"])


def _subtitle_position(style_preset: str, explicit_position: dict[str, float] | None):
    if explicit_position is not None:
        return explicit_position
    return dict(_subtitle_preset(style_preset)["position"])


def _derive_overlay_events(emphasis: list[EmphasisHint], units: list[dict]) -> list[OverlayEvent]:
    """Place each emphasis phrase onto the narration sentence that contains it.

    Deterministic substring match (whitespace/case-insensitive) against the real
    narration timeline; the matched sentence supplies the timing, the phrase itself is
    the overlay text. A phrase matching no sentence is dropped: emphasis is an additive
    花字 overlay, so a miss leaves the baseline subtitles untouched rather than degrading
    them (hence no DegradationNotice). At most one overlay per narration sentence (two
    phrases sharing a sentence would render as same-time, same-position banners), so a
    later phrase whose only match is an already-claimed sentence is dropped. Phrases keep
    the LLM's order.
    """
    events: list[OverlayEvent] = []
    used: set[int] = set()
    for hint in emphasis:
        needle = _compact_text(hint.phrase)
        if not needle:
            continue
        for index, unit in enumerate(units):
            if index in used:
                continue
            if needle in _compact_text(str(unit.get("text", ""))):
                used.add(index)
                events.append(
                    OverlayEvent(
                        start=float(unit.get("start", 0) or 0),
                        end=float(unit.get("end", 0) or 0),
                        text=hint.phrase,
                    )
                )
                break
    return events


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


def _is_segmented_bgm_candidate(candidate: dict) -> bool:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    if not _str_or_none(metadata.get("clip_id")):
        return False
    source_start = _float_or_none(metadata.get("source_start"))
    source_end = _float_or_none(metadata.get("source_end"))
    duration = _float_or_none(metadata.get("duration"))
    return (
        source_start is not None
        and source_end is not None
        and duration is not None
        and source_end > source_start
        and duration > 0
    )


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
    return base + positive * 50.0 - negative * 80.0 + _single_clip_usability_score(metadata)


def _single_clip_usability_score(metadata: dict) -> float:
    """Prefer macro BGM sections that can carry a whole short video by themselves."""
    duration = _float_or_none(metadata.get("duration")) or 0.0
    loopable = _bool_from_metadata(metadata.get("loopable"))
    section_type = str(metadata.get("section_type") or "")
    score = 0.0
    if duration >= 60.0:
        score += 45.0
    elif duration >= 36.0:
        score += 25.0
    elif duration < 24.0:
        score -= 70.0
    if loopable:
        score += 20.0
    elif duration < 45.0:
        score -= 60.0
    if section_type in {"stable_bed", "loop", "verse", "chorus", "drop"}:
        score += 12.0
    if section_type in {"intro", "outro"} and duration < 36.0:
        score -= 30.0
    return score


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
