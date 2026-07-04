"""Pure media-assignment materializers shared by deterministic and agent paths.

``TimelineWindowPlanning`` in the v2 chain publishes
``default_assignment.portrait_plan_payload`` byte-for-byte as ``plan.portrait``:
that payload is the Phase 1 golden output, and rebuilding it here would risk
changing field order or diagnostics. The LLM agent path uses the portrait
materializer below because it starts from a media assignment rather than the
compiler's prebuilt portrait artifact.

The B-roll and style helpers are shared by both paths. They contain no
``NodeContext`` access, repository IO, provider calls, or artifact writes.
"""

from __future__ import annotations

from typing import Any

from packages.core.contracts import DegradationNotice, WarningCode
from packages.core.contracts.artifacts import (
    BgmPlan,
    BrollOverlay,
    BrollPlanArtifact,
    FontPlan,
    OverlayEvent,
    PortraitPlanArtifact,
    PortraitSegment,
    StylePlanArtifact,
    SubtitleStylePlan,
)
from packages.planning.editing.frame_grid import (
    FrameWindow,
    slice_source_window,
    to_seconds,
)
from packages.planning.material import longest_clean_portrait_source_span
from packages.planning.material.broll_plan import (
    BROLL_GEOMETRY_POLICY,
    BrollGeometryPolicy,
    BrollInsertion,
    place_insertion_safely,
)

TIMELINE_FPS = 30

_SUBTITLE_PRESET_DEFAULTS = {
    "douyin": {"font_size": 64, "position": {"x": 0.5, "y": 0.88}},
    "clean": {"font_size": 52, "position": {"x": 0.5, "y": 0.86}},
    "variety": {"font_size": 68, "position": {"x": 0.5, "y": 0.84}},
    "news": {"font_size": 48, "position": {"x": 0.5, "y": 0.90}},
    "movie": {"font_size": 44, "position": {"x": 0.5, "y": 0.82}},
    "youshe_title_black": {"font_size": 66, "position": {"x": 0.5, "y": 0.86}},
}


def materialize_portrait_from_assignment(
    *,
    windows: dict,
    assignment: dict,
    candidates,
) -> dict:
    choice_by_window = {
        _as_str(item.get("window_id")): item
        for item in (assignment.get("portrait") or [])
        if isinstance(item, dict) and _as_str(item.get("window_id"))
    }
    candidate_by_id = _candidate_group(candidates, "portrait_by_id")
    portrait_windows = sorted(
        (window for window in (windows.get("portrait_windows") or []) if isinstance(window, dict)),
        key=lambda window: int(window.get("start_frame", 0) or 0),
    )

    segments: list[PortraitSegment] = []
    for index, window_data in enumerate(portrait_windows):
        choice = choice_by_window.get(_as_str(window_data.get("window_id")))
        if choice is None:
            continue
        candidate = candidate_by_id.get(_as_str(choice.get("candidate_id")))
        if candidate is None:
            continue
        meta = _meta(candidate)
        window = FrameWindow(
            start_frame=int(window_data.get("start_frame", 0) or 0),
            end_frame=int(window_data.get("end_frame", 0) or 0),
        )
        clean_span = longest_clean_portrait_source_span(meta)
        if clean_span is None:
            continue
        source_start, source_end = clean_span
        source_window, _pad_end = slice_source_window(
            source_start_seconds=source_start,
            length_frames=window.length_frames,
            source_window_start_seconds=source_start,
            source_window_end_seconds=source_end,
        )
        phase = _as_str(window_data.get("phase")).lower()
        segments.append(
            PortraitSegment(
                segment_id=f"portrait_{index + 1}",
                asset_id=_as_str(candidate.get("asset_id")) or None,
                clip_id=_as_str(meta.get("clip_id")) or None,
                start_sec=to_seconds(window.start_frame),
                end_sec=to_seconds(window.end_frame),
                source_start=to_seconds(source_window.start_frame),
                source_end=to_seconds(source_window.end_frame),
                role="main",
                source_mode=_as_str(choice.get("source_mode")) or "lipsynced",
                boundary_source=_as_str(window_data.get("boundary_source")) or None,
                boundary_reason=None,
                unit_ids=[_as_str(unit_id) for unit_id in (window_data.get("unit_ids") or [])],
                slot_phase="portrait_opening"
                if index == 0 or phase == "opening"
                else "portrait_main",
                recently_used_material=False,
                timeline_start_frame=window.start_frame,
                timeline_end_frame=window.end_frame,
                source_start_frame=source_window.start_frame,
                source_end_frame=source_window.end_frame,
            )
        )

    total_frames = segments[-1].timeline_end_frame if segments else 0
    total_duration = round(to_seconds(total_frames), 3)
    return PortraitPlanArtifact(
        fps=TIMELINE_FPS,
        total_duration=total_duration,
        asset_id=segments[0].asset_id if segments else None,
        duration_sec=total_duration,
        segments=segments,
        diagnostics={"planner": "editing_agent", "segment_count": len(segments)},
    ).model_dump(mode="json")


def portrait_cut_frames(portrait_payload: dict) -> list[int]:
    return sorted(
        {
            int(frame)
            for segment in portrait_payload.get("segments", [])
            for frame in (
                segment.get("timeline_start_frame"),
                segment.get("timeline_end_frame"),
            )
            if frame is not None
        }
    )


def materialize_broll_from_assignment(
    *,
    windows: dict,
    assignment: dict,
    candidates,
    cut_frames: list[int],
    enabled: bool,
    max_inserts: int,
    policy: BrollGeometryPolicy = BROLL_GEOMETRY_POLICY,
) -> tuple[dict, list[dict]]:
    if not enabled:
        return BrollPlanArtifact(enabled=False).model_dump(mode="json"), []

    fps = int(windows.get("fps") or TIMELINE_FPS)
    window_by_id = {
        _as_str(window.get("window_id")): window
        for window in (windows.get("broll_windows") or [])
        if isinstance(window, dict)
    }
    candidate_by_id = _candidate_group(candidates, "broll_by_id")
    accepted: list[BrollInsertion] = []
    drop_diagnostics: list[dict] = []
    for choice in (assignment.get("broll") or [])[: max(0, max_inserts)]:
        if not isinstance(choice, dict):
            continue
        window_id = _as_str(choice.get("window_id"))
        candidate_id = _as_str(choice.get("candidate_id"))
        window_data = window_by_id.get(window_id)
        candidate = candidate_by_id.get(candidate_id)
        drop_base = {"slot_id": window_id, "candidate_id": candidate_id}
        if window_data is None:
            drop_diagnostics.append({**drop_base, "reason": "unknown_slot"})
            continue
        if candidate is None:
            drop_diagnostics.append({**drop_base, "reason": "unknown_candidate"})
            continue

        meta = _meta(candidate)
        timeline_start = int(window_data.get("start_frame", 0) or 0) / fps
        timeline_end = int(window_data.get("end_frame", 0) or 0) / fps
        source_start = _as_float(meta.get("source_start"))
        source_end = _as_float(meta.get("source_end"))
        slot_span = max(0.0, timeline_end - timeline_start)
        source_available = source_end - source_start
        if 0.0 < source_available < policy.min_insert_seconds:
            drop_diagnostics.append({**drop_base, "reason": "source_too_short"})
            continue
        source_limit = source_available if source_available > 0.0 else policy.max_insert_seconds
        span = min(policy.max_insert_seconds, slot_span, source_limit)
        if span < policy.min_insert_seconds:
            drop_diagnostics.append({**drop_base, "reason": "slot_too_short"})
            continue

        insert = BrollInsertion(
            asset_id=_as_str(candidate.get("asset_id")),
            clip_id=_as_str(meta.get("clip_id")),
            timeline_start=timeline_start,
            timeline_end=round(timeline_start + span, 3),
            source_start=source_start,
            source_end=round(source_start + span, 3),
            confidence=_as_float(choice.get("confidence")),
            matched_keywords=tuple(
                _as_str(keyword)
                for keyword in (choice.get("matched_keywords") or [])
                if _as_str(keyword)
            ),
            scene_name=_as_str(meta.get("scene_name")),
            reason=_as_str(choice.get("reason")) or "editing agent selection",
            diversity_key=_as_str(meta.get("diversity_key")),
        )
        next_accepted = place_insertion_safely(
            accepted,
            insert,
            window_start=timeline_start,
            window_end=timeline_end,
            fps=fps,
            portrait_cut_frames=cut_frames,
            policy=policy,
        )
        if next_accepted is None:
            drop_diagnostics.append({**drop_base, "reason": "geometry_rejected"})
            continue
        accepted = next_accepted

    return (
        BrollPlanArtifact(enabled=True, overlays=overlays_from_insertions(accepted)).model_dump(
            mode="json"
        ),
        drop_diagnostics,
    )


def overlays_from_insertions(insertions: list[BrollInsertion]) -> list[BrollOverlay]:
    return [
        BrollOverlay(
            overlay_id=f"broll_{index + 1}",
            asset_id=insertion.asset_id,
            clip_id=insertion.clip_id,
            timeline_start=insertion.timeline_start,
            timeline_end=insertion.timeline_end,
            source_start=insertion.source_start,
            source_end=insertion.source_end,
            timeline_start_frame=insertion.timeline_start_frame,
            timeline_end_frame=insertion.timeline_end_frame,
            source_start_frame=insertion.source_start_frame,
            source_end_frame=insertion.source_end_frame,
            pad_start=insertion.pad_start,
            pad_end=insertion.pad_end,
            reason=insertion.reason,
            confidence=insertion.confidence,
            matched_keywords=list(insertion.matched_keywords),
            scene_name=insertion.scene_name,
            diversity_key=insertion.diversity_key or None,
        )
        for index, insertion in enumerate(insertions)
    ]


def materialize_style_from_selection(
    *,
    request,
    material: dict,
    overlay_events: list[OverlayEvent],
    font_id: str | None = None,
    bgm_id: str | None = None,
) -> tuple[dict, list[WarningCode], list[DegradationNotice]]:
    font_candidates = [
        item for item in (material.get("font_candidates") or []) if item.get("asset_id")
    ]
    raw_bgm_candidates = [
        item for item in (material.get("bgm_candidates") or []) if item.get("asset_id")
    ]
    bgm_candidates = [item for item in raw_bgm_candidates if _is_segmented_bgm_candidate(item)]

    warnings: list[WarningCode] = []
    degradations: list[DegradationNotice] = []
    font_asset_id = _selected_font_id(font_candidates, font_id)
    if not font_candidates:
        warnings.append(WarningCode.font_default_used)

    selected_bgm = (
        _select_bgm_candidate(
            bgm_candidates,
            requested_asset_id=bgm_id or request.bgm.bgm_id,
            script=request.script,
        )
        if request.bgm.enabled
        else None
    )
    bgm_asset_id = selected_bgm.get("asset_id") if selected_bgm else None
    if request.bgm.enabled and not bgm_asset_id:
        degradations.append(
            DegradationNotice(
                code=WarningCode.bgm_skipped_library_unannotated,
                message="BGM library is not annotated.",
                affects_true_yield=False,
            )
        )
        warnings.append(WarningCode.bgm_skipped_library_unannotated)

    bgm_metadata = selected_bgm.get("metadata") if isinstance(selected_bgm, dict) else {}
    if not isinstance(bgm_metadata, dict):
        bgm_metadata = {}
    payload = StylePlanArtifact(
        subtitle=SubtitleStylePlan(
            font_id=request.subtitle.font_id,
            font_size=_subtitle_font_size(
                request.subtitle.style_preset,
                request.subtitle.font_size,
            ),
            position=_subtitle_position(
                request.subtitle.style_preset,
                request.subtitle.position,
            ),
        ),
        bgm=BgmPlan(
            enabled=request.bgm.enabled,
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
            volume=request.bgm.volume,
            auto_mix=request.bgm.auto_mix,
        ),
        font=FontPlan(font_id=font_asset_id),
        font_asset_id=font_asset_id,
        bgm_asset_id=bgm_asset_id,
        overlay_events=overlay_events,
    ).model_dump(mode="json")
    return payload, warnings, degradations


def _candidate_group(candidates, attribute: str) -> dict[str, dict]:
    group = getattr(candidates, attribute, None)
    if isinstance(group, dict):
        return group
    if isinstance(candidates, dict):
        value = candidates.get(attribute)
        if isinstance(value, dict):
            return value
    return {}


def _selected_font_id(candidates: list[dict], font_id: str | None) -> str:
    candidate_ids = [str(candidate.get("asset_id") or "") for candidate in candidates]
    if font_id and font_id in candidate_ids:
        return font_id
    if candidate_ids:
        return candidate_ids[0]
    return "case_default_font"


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


def _meta(candidate: dict) -> dict[str, Any]:
    meta = candidate.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""
