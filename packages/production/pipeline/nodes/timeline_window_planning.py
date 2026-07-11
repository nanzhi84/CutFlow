"""Compile authoritative windows and keep the compiler portrait only as a nested default.

The node never publishes the final ``plan.portrait`` artifact. The downstream
deterministic or Agent media-selection node is its single writer for each workflow.
"""

from __future__ import annotations

from dataclasses import asdict

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.contracts.artifacts import PortraitPlanArtifact, TimelineWindowsPlan
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing import (
    TIMELINE_FPS,
    BoundaryConstraints,
    frame_index,
    plan_boundary_timeline,
)
from packages.planning.material import (
    BROLL_GEOMETRY_POLICY,
    clean_portrait_source_windows,
    legalize_broll_window_frames,
)
from packages.production.pipeline._narration_units import build_planner_narration_units
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._broll_policy import broll_full_coverage_enabled


_FULL_COVERAGE_CUT_PRIORITY = {
    "audio_pause": 0,
    "safe_cut": 0,
    "semantic_group": 0,
    "unit_boundary": 2,
    "fallback": 3,
}

_FULL_COVERAGE_GROUP_PAUSE_MS = 120
_FULL_COVERAGE_GROUP_BOUNDARY_SCORE = 0.62


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    boundary = state.require(ArtifactKind.plan_narration_boundary).payload or {}
    raw_units = narration.get("units", []) or []
    duration = max([float(unit.get("end", 0)) for unit in raw_units] or [1.0])

    planner_units = build_planner_narration_units(
        raw_units=raw_units,
        source=str(narration.get("source") or ""),
        script=state.request.script,
        duration=duration,
    )
    audio_pauses = boundary.get("pause_windows", []) or []

    if state.request.broll.mode == "full_coverage" and broll_full_coverage_enabled(state.request):
        return _full_coverage_output(
            ctx=ctx,
            planner_units=planner_units,
            boundary=boundary,
            duration=duration,
            audio_pauses=audio_pauses,
            broll_candidate_frames=_broll_candidate_source_frames(
                material.get("broll_candidates") or []
            ),
        )

    hard_fail = state.request.strictness.portrait_insufficient_policy == "hard_fail"
    portrait_candidate_items = [
        item for item in material.get("portrait_candidates", []) if item.get("asset_id")
    ]
    if hard_fail and not portrait_candidate_items:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "Portrait main track cannot cover the full audio.",
        )

    candidates = _portrait_window_candidates(portrait_candidate_items)
    if portrait_candidate_items and not candidates:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "Portrait source window cannot cover the full audio.",
        )

    plan, escalation = _plan_with_escalation(
        narration_units=planner_units,
        candidates=candidates,
        duration=duration,
        audio_pauses=audio_pauses or None,
    )
    if not plan.ok:
        distinct_assets = sorted({str(c.get("template_id") or "") for c in candidates if c})
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "Portrait main track cannot cover the full audio under asset-level "
            "uniqueness (each portrait asset is used at most once per run).",
            details={
                "reason": "portrait_coverage_insufficient_under_asset_uniqueness",
                "target_duration_sec": round(duration, 3),
                "distinct_portrait_asset_count": len(distinct_assets),
                "candidate_window_count": len(candidates),
                "longest_usable_source_window": escalation["longest_usable_source_window"],
                "recovery_stage": escalation["stage"],
                "recovery_attempts": escalation["attempts"],
            },
        )

    recent_template_ids = {
        str(c.get("template_id") or "")
        for c in candidates
        if isinstance(c.get("recent_usage"), dict) and c["recent_usage"].get("is_recently_used")
    }
    segments = [
        _segment_payload(index, seg, recent_template_ids=recent_template_ids)
        for index, seg in enumerate(plan.segments)
    ]
    total_duration = round(plan.total_frames / TIMELINE_FPS, 3)
    portrait_plan_payload = PortraitPlanArtifact(
        fps=TIMELINE_FPS,
        total_duration=total_duration,
        asset_id=segments[0]["asset_id"] if segments else None,
        duration_sec=total_duration,
        segments=segments,
        diagnostics={
            "used_audio_pauses": plan.used_audio_pauses,
            "audio_pause_count": len(audio_pauses),
            "segment_count": len(segments),
            "recovery_stage": escalation["stage"],
            "recovery_attempts": escalation["attempts"],
            "capacity_controlled_split": escalation["capacity_controlled_split"],
            "longest_usable_source_window": escalation["longest_usable_source_window"],
            "audio_pause_capacity_cap": escalation.get("audio_pause_capacity_cap"),
            "recently_used_segment_count": sum(
                1 for seg in segments if seg.get("recently_used_material")
            ),
        },
    ).model_dump(mode="json")
    portrait_windows = _portrait_windows(plan.segments)
    payload = TimelineWindowsPlan(
        fps=TIMELINE_FPS,
        total_frames=plan.total_frames,
        geometry_policy={
            "broll": asdict(BROLL_GEOMETRY_POLICY),
            "broll_window_contract": {
                "authority": "TimelineWindowPlanning",
                "semantics": "authoritative_optional_placement_slot",
                "downstream_may_skip": True,
                "downstream_may_resize": False,
            },
            "portrait_reuse": {"mode": "asset_level_unique", "max_uses": 1},
        },
        portrait_windows=portrait_windows,
        broll_windows=_broll_windows(boundary.get("broll_slots") or [], portrait_windows),
        default_assignment={
            "portrait": [
                {
                    "window_id": str(seg.window_id or ""),
                    "segment_payload": segment_payload,
                }
                for seg, segment_payload in zip(plan.segments, segments)
            ],
            "portrait_plan_payload": portrait_plan_payload,
            "engine": "compiler_default",
        },
        compile_diagnostics={
            "recovery_stage": escalation["stage"],
            "attempts": escalation["attempts"],
            "capacity_controlled_split": escalation["capacity_controlled_split"],
            "longest_usable_source_window": escalation["longest_usable_source_window"],
            "audio_pause_capacity_cap": escalation.get("audio_pause_capacity_cap"),
            "requested_constraints": {
                "target_duration": round(duration, 3),
                "fps": TIMELINE_FPS,
            },
            "used_audio_pauses": plan.used_audio_pauses,
            "audio_pause_count": len(audio_pauses),
            "segment_count": len(segments),
        },
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_timeline_windows,
                payload,
                "TimelineWindowsPlan.v1",
            )
        ]
    )


def _full_coverage_output(
    *,
    ctx: NodeContext,
    planner_units,
    boundary: dict,
    duration: float,
    audio_pauses: list[dict],
    broll_candidate_frames: list[int],
) -> NodeOutput:
    total_frames = max(1, int(boundary.get("total_frames") or frame_index(duration)))
    min_segment_frames = max(1, frame_index(ctx.state.request.broll.min_segment_duration))
    if not broll_candidate_frames:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_broll,
            "B-roll full coverage requires at least one eligible B-roll candidate.",
            details={
                "reason": "full_coverage_no_broll_candidates",
                "target_duration_sec": round(duration, 3),
            },
        )
    longest_candidate_frames = max(broll_candidate_frames)
    if longest_candidate_frames < min_segment_frames:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_broll,
            "B-roll candidate source windows are too short for full coverage planning.",
            details={
                "reason": "full_coverage_broll_capacity_below_min_segment",
                "min_segment_frames": min_segment_frames,
                "longest_candidate_frames": longest_candidate_frames,
                "candidate_count": len(broll_candidate_frames),
            },
        )
    broll_windows, diagnostics = compile_full_coverage_broll_windows(
        narration_units=planner_units,
        pause_windows=audio_pauses,
        safe_cut_boundaries=boundary.get("safe_cut_boundaries") or [],
        total_frames=total_frames,
        min_segment_duration=ctx.state.request.broll.min_segment_duration,
        max_source_frames_available=longest_candidate_frames,
        fps=TIMELINE_FPS,
    )
    oversized_windows = [
        {
            "window_id": str(window.get("window_id") or ""),
            "length_frames": int(window.get("length_frames", 0) or 0),
        }
        for window in broll_windows
        if int(window.get("length_frames", 0) or 0) > longest_candidate_frames
    ]
    if oversized_windows:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_broll,
            "B-roll full coverage windows cannot be covered by available candidates.",
            details={
                "reason": "full_coverage_broll_window_exceeds_candidate_capacity",
                "longest_candidate_frames": longest_candidate_frames,
                "oversized_windows": oversized_windows,
                "candidate_count": len(broll_candidate_frames),
            },
        )
    total_duration = round(total_frames / TIMELINE_FPS, 3)
    portrait_plan_payload = PortraitPlanArtifact(
        fps=TIMELINE_FPS,
        total_duration=total_duration,
        asset_id=None,
        duration_sec=total_duration,
        segments=[],
        diagnostics={
            "track_mode": "broll_full_coverage",
            "skipped_reason": "broll.full_coverage",
        },
    ).model_dump(mode="json")
    payload = TimelineWindowsPlan(
        fps=TIMELINE_FPS,
        total_frames=total_frames,
        geometry_policy={
            "broll": asdict(BROLL_GEOMETRY_POLICY),
            "broll_window_contract": {
                "authority": "TimelineWindowPlanning",
                "semantics": "authoritative_full_coverage_main_visual_track",
                "downstream_may_skip": False,
                "downstream_may_resize": False,
                "downstream_may_stitch": False,
            },
            "portrait_reuse": {"mode": "disabled_for_full_coverage"},
        },
        portrait_windows=[],
        broll_windows=broll_windows,
        default_assignment={
            "portrait": [],
            "portrait_plan_payload": portrait_plan_payload,
            "engine": "compiler_full_coverage",
        },
        compile_diagnostics={
            "track_mode": "broll_full_coverage",
            "requested_constraints": {
                "target_duration": round(duration, 3),
                "fps": TIMELINE_FPS,
                "min_segment_duration": ctx.state.request.broll.min_segment_duration,
                "max_segment_duration": (
                    BROLL_GEOMETRY_POLICY.full_coverage_max_segment_seconds
                ),
                "candidate_max_segment_seconds": round(
                    longest_candidate_frames / TIMELINE_FPS,
                    3,
                ),
            },
            "used_audio_pauses": bool(
                diagnostics.get("selected_cut_source_counts", {}).get("audio_pause", 0)
            ),
            "audio_pause_count": len(audio_pauses),
            **diagnostics,
        },
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_timeline_windows,
                payload,
                "TimelineWindowsPlan.v1",
            )
        ]
    )


def compile_full_coverage_broll_windows(
    *,
    narration_units,
    pause_windows: list[dict],
    safe_cut_boundaries: list[dict],
    total_frames: int,
    min_segment_duration: float,
    max_source_frames_available: int | None = None,
    fps: int = TIMELINE_FPS,
) -> tuple[list[dict], dict]:
    total_frames = max(1, int(total_frames))
    min_frames = max(1, frame_index(float(min_segment_duration)))
    policy_max_frames = max(
        min_frames,
        frame_index(BROLL_GEOMETRY_POLICY.full_coverage_max_segment_seconds),
    )
    source_cap_frames = (
        int(max_source_frames_available or 0)
        if max_source_frames_available is not None
        else None
    )
    max_frames = policy_max_frames
    if source_cap_frames is not None and source_cap_frames > 0:
        max_frames = max(min_frames, min(policy_max_frames, source_cap_frames))
    semantic_groups = _full_coverage_semantic_groups(narration_units)
    candidates = _full_coverage_cut_candidates(
        narration_units=narration_units,
        semantic_groups=semantic_groups,
        safe_cut_boundaries=safe_cut_boundaries,
        total_frames=total_frames,
    )
    diagnostics = {
        "candidate_cut_count": len(
            [frame for frame in candidates if 0 < frame < total_frames]
        ),
        "candidate_cut_source_counts": _cut_source_counts(candidates),
        "fallback_cut_count": 0,
        "min_segment_frames": min_frames,
        "max_segment_frames": max_frames,
        "policy_max_segment_frames": policy_max_frames,
        "candidate_max_source_frames": source_cap_frames,
        "raw_pause_window_count": len(pause_windows),
        "semantic_group_count": len(semantic_groups),
        "semantic_group_boundary_count": max(0, len(semantic_groups) - 1),
        "semantic_group_policy": {
            "pause_after_ms": _FULL_COVERAGE_GROUP_PAUSE_MS,
            "boundary_score": _FULL_COVERAGE_GROUP_BOUNDARY_SCORE,
        },
    }
    cuts = [0]
    cursor = 0
    while total_frames - cursor > max_frames:
        cut_frame, source = _choose_full_coverage_cut(
            cursor=cursor,
            total_frames=total_frames,
            min_frames=min_frames,
            max_frames=max_frames,
            candidates=candidates,
        )
        if cut_frame <= cursor or cut_frame >= total_frames:
            break
        if source == "fallback":
            diagnostics["fallback_cut_count"] += 1
            candidates[cut_frame] = {
                "frame": cut_frame,
                "priority": _FULL_COVERAGE_CUT_PRIORITY["fallback"],
                "sources": ["fallback"],
            }
        cuts.append(cut_frame)
        cursor = cut_frame
    cuts.append(total_frames)

    spans = [(start_frame, end_frame) for start_frame, end_frame in zip(cuts, cuts[1:])]
    text_assignments = _full_coverage_text_assignments(narration_units, spans=spans)
    windows: list[dict] = []
    for index, (start_frame, end_frame) in enumerate(spans):
        if end_frame <= start_frame:
            continue
        host_unit_ids = _full_coverage_window_unit_ids(
            narration_units,
            start_frame=start_frame,
            end_frame=end_frame,
        )
        length_frames = end_frame - start_frame
        text = " ".join(text_assignments["text_parts"][index]).strip()
        windows.append(
            {
                "window_id": f"bwin_{index:03d}",
                "start_frame": start_frame,
                "end_frame": end_frame,
                "length_frames": length_frames,
                "source_length_frames": length_frames,
                "host_unit_ids": host_unit_ids,
                "text": text,
                "text_assignment": "argmax_overlap",
                "scene_hint": _full_coverage_scene_hint(text),
            }
        )
    diagnostics["window_count"] = len(windows)
    diagnostics["cut_frames"] = cuts
    diagnostics["selected_cut_source_counts"] = _selected_cut_source_counts(cuts, candidates)
    diagnostics["split_unit_count"] = text_assignments["split_unit_count"]
    return windows, diagnostics


def _broll_candidate_source_frames(candidates: list[dict]) -> list[int]:
    frames: list[int] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        meta = candidate.get("metadata")
        if not isinstance(meta, dict):
            continue
        raw_frames = meta.get("source_frames_available")
        if raw_frames is not None and not isinstance(raw_frames, bool):
            try:
                source_frames = int(raw_frames)
            except (TypeError, ValueError):
                source_frames = 0
            if source_frames > 0:
                frames.append(source_frames)
                continue
        try:
            source_start = float(meta.get("source_start") or 0.0)
            source_end = float(meta.get("source_end") or 0.0)
        except (TypeError, ValueError):
            continue
        source_frames = max(0, frame_index(source_end) - frame_index(source_start))
        if source_frames > 0:
            frames.append(source_frames)
    return frames


def _full_coverage_cut_candidates(
    *,
    narration_units,
    semantic_groups: list[dict],
    safe_cut_boundaries: list[dict],
    total_frames: int,
) -> dict[int, dict]:
    candidates: dict[int, dict] = {}

    def add(frame: int, source: str) -> None:
        frame = max(0, min(total_frames, int(frame)))
        priority = _FULL_COVERAGE_CUT_PRIORITY[source]
        current = candidates.get(frame)
        if current is None or priority < int(current["priority"]):
            candidates[frame] = {"frame": frame, "priority": priority, "sources": [source]}
        elif source not in current["sources"]:
            current["sources"].append(source)

    add(0, "unit_boundary")
    add(total_frames, "unit_boundary")
    for cut in safe_cut_boundaries:
        if not isinstance(cut, dict):
            continue
        raw_frame = cut.get("frame")
        if raw_frame is None and cut.get("time") is not None:
            raw_frame = frame_index(float(cut["time"]))
        if raw_frame is None:
            continue
        source = str(cut.get("source") or "")
        add(int(raw_frame), "audio_pause" if "pause" in source else "safe_cut")
    for unit in narration_units:
        add(frame_index(float(unit.start)), "unit_boundary")
        add(frame_index(float(unit.end)), "unit_boundary")
    for group in semantic_groups[:-1]:
        add(int(group["end_frame"]), "semantic_group")
    return candidates


def _full_coverage_semantic_groups(narration_units) -> list[dict]:
    groups: list[dict] = []
    current_units = []
    for unit in narration_units:
        current_units.append(unit)
        if _full_coverage_is_semantic_group_boundary(unit):
            groups.append(_full_coverage_group_payload(current_units))
            current_units = []
    if current_units:
        groups.append(_full_coverage_group_payload(current_units))
    return groups


def _full_coverage_group_payload(units) -> dict:
    start_frame = frame_index(float(units[0].start))
    end_frame = frame_index(float(units[-1].end))
    return {
        "start_frame": start_frame,
        "end_frame": end_frame,
        "unit_ids": [str(unit.unit_id) for unit in units],
    }


def _full_coverage_is_semantic_group_boundary(unit) -> bool:
    pause_after_ms = int(getattr(unit, "pause_after_ms", 0))
    boundary_score = float(getattr(unit, "boundary_score", 0.0))
    return bool(getattr(unit, "hard_end", False)) or (
        pause_after_ms >= _FULL_COVERAGE_GROUP_PAUSE_MS
        or boundary_score >= _FULL_COVERAGE_GROUP_BOUNDARY_SCORE
    )


def _choose_full_coverage_cut(
    *,
    cursor: int,
    total_frames: int,
    min_frames: int,
    max_frames: int,
    candidates: dict[int, dict],
) -> tuple[int, str]:
    lower = cursor + min_frames
    upper = min(cursor + max_frames, total_frames - min_frames)
    if upper < lower:
        return total_frames, "remainder"
    natural = [
        item for frame, item in candidates.items() if lower <= frame <= upper
    ]
    if natural:
        midpoint = lower + (upper - lower) / 2
        best = sorted(
            natural,
            key=lambda item: (
                int(item["priority"]),
                abs(int(item["frame"]) - midpoint),
                int(item["frame"]),
            ),
        )[0]
        return int(best["frame"]), str(best["sources"][0])
    fallback = max(lower, min(upper, cursor + max_frames))
    return fallback, "fallback"


def _full_coverage_window_unit_ids(
    narration_units,
    *,
    start_frame: int,
    end_frame: int,
) -> list[str]:
    unit_ids: list[str] = []
    for unit in narration_units:
        unit_start = frame_index(float(unit.start))
        unit_end = frame_index(float(unit.end))
        if unit_end <= start_frame or unit_start >= end_frame:
            continue
        unit_ids.append(str(unit.unit_id))
    return unit_ids


def _full_coverage_text_assignments(narration_units, *, spans: list[tuple[int, int]]):
    text_parts: list[list[str]] = [[] for _ in spans]
    split_unit_count = 0
    for unit in narration_units:
        unit_start = frame_index(float(unit.start))
        unit_end = frame_index(float(unit.end))
        if unit_end <= unit_start:
            continue
        overlaps: list[tuple[int, int]] = []
        for index, (start_frame, end_frame) in enumerate(spans):
            overlap = max(0, min(unit_end, end_frame) - max(unit_start, start_frame))
            if overlap > 0:
                overlaps.append((index, overlap))
        if not overlaps:
            continue
        if len(overlaps) > 1:
            split_unit_count += 1
        assigned_index, _ = max(overlaps, key=lambda item: (item[1], -item[0]))
        text = str(unit.text).strip()
        if text:
            text_parts[assigned_index].append(text)
    return {
        "text_parts": text_parts,
        "split_unit_count": split_unit_count,
    }


def _full_coverage_scene_hint(text: str) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:120]


def _cut_source_counts(candidates: dict[int, dict]) -> dict[str, int]:
    counts = {key: 0 for key in _FULL_COVERAGE_CUT_PRIORITY}
    for item in candidates.values():
        for source in item.get("sources") or []:
            counts[str(source)] = counts.get(str(source), 0) + 1
    return counts


def _selected_cut_source_counts(cuts: list[int], candidates: dict[int, dict]) -> dict[str, int]:
    counts = {key: 0 for key in _FULL_COVERAGE_CUT_PRIORITY}
    for frame in cuts[1:-1]:
        item = candidates.get(frame) or {}
        source = str((item.get("sources") or ["fallback"])[0])
        counts[source] = counts.get(source, 0) + 1
    return counts


def _plan_with_escalation(
    *,
    narration_units,
    candidates: list[dict],
    duration: float,
    audio_pauses,
):
    """Drive the portrait-insufficiency escalation ladder before giving up."""
    attempts: list[dict] = []
    longest_usable = max((float(c.get("duration") or 0.0) for c in candidates), default=0.0)

    plan = plan_boundary_timeline(
        narration_units=narration_units,
        portrait_candidates=candidates,
        constraints=BoundaryConstraints(target_duration=duration),
        audio_pauses=audio_pauses,
        fps=TIMELINE_FPS,
    )
    attempts.append({"stage": "full_pool", "ok": plan.ok})
    if plan.ok:
        return plan, {
            "stage": "full_pool",
            "attempts": attempts,
            "capacity_controlled_split": False,
            "longest_usable_source_window": round(longest_usable, 3),
            "audio_pause_capacity_cap": None,
        }

    if longest_usable > 0.08:
        split_plan = plan_boundary_timeline(
            narration_units=narration_units,
            portrait_candidates=candidates,
            constraints=BoundaryConstraints(
                target_duration=duration,
                max_chunk_duration=round(longest_usable, 3),
            ),
            audio_pauses=audio_pauses,
            fps=TIMELINE_FPS,
        )
        attempts.append({"stage": "capacity_controlled_split", "ok": split_plan.ok})
        if split_plan.ok:
            return split_plan, {
                "stage": "capacity_controlled_split",
                "attempts": attempts,
                "capacity_controlled_split": True,
                "longest_usable_source_window": round(longest_usable, 3),
                "audio_pause_capacity_cap": None,
            }
        if audio_pauses and longest_usable > 8.05:
            pause_cap = 8.0
            pause_split_plan = plan_boundary_timeline(
                narration_units=narration_units,
                portrait_candidates=candidates,
                constraints=BoundaryConstraints(
                    target_duration=duration,
                    max_chunk_duration=pause_cap,
                ),
                audio_pauses=audio_pauses,
                fps=TIMELINE_FPS,
            )
            attempts.append(
                {
                    "stage": "audio_pause_capacity_split",
                    "ok": pause_split_plan.ok,
                    "max_chunk_duration": pause_cap,
                }
            )
            if pause_split_plan.ok:
                return pause_split_plan, {
                    "stage": "audio_pause_capacity_split",
                    "attempts": attempts,
                    "capacity_controlled_split": True,
                    "longest_usable_source_window": round(longest_usable, 3),
                    "audio_pause_capacity_cap": pause_cap,
                }

    return plan, {
        "stage": "exhausted",
        "attempts": attempts,
        "capacity_controlled_split": False,
        "longest_usable_source_window": round(longest_usable, 3),
        "audio_pause_capacity_cap": None,
    }


def _portrait_window_candidates(items: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    for rank, item in enumerate(items):
        asset_id = item.get("asset_id")
        if not asset_id:
            continue
        meta = item.get("metadata") or {}
        clip_id = meta.get("clip_id")
        if clip_id is None or meta.get("source_start") is None or meta.get("source_end") is None:
            continue
        clean_windows = (
            clean_portrait_source_windows(meta)
            if meta.get("avoid_spans") is not None
            else _raw_portrait_source_windows(meta)
        )
        if not clean_windows:
            continue
        recent_usage = meta.get("recent_usage")
        if not isinstance(recent_usage, dict):
            recent_usage = {}
        recency_penalty = recent_usage.get("recency_penalty", meta.get("recency_penalty", 0.0))
        confidence = round(max(0.1, 0.9 - rank * 0.05), 3)
        source_window_id = str(meta.get("source_window_id") or clip_id)
        for window_index, (win_start, win_end) in enumerate(clean_windows):
            window_suffix = "" if window_index == 0 else f":m{window_index}"
            candidates.append(
                {
                    "window_id": f"{asset_id}:{source_window_id}{window_suffix}",
                    "template_id": asset_id,
                    "template_name": asset_id,
                    "start": round(win_start, 3),
                    "end": round(win_end, 3),
                    "duration": round(win_end - win_start, 3),
                    "role": "main",
                    "confidence": confidence,
                    "source_mode_hint": "lipsynced",
                    "recent_usage": recent_usage,
                    "recency_penalty": recency_penalty,
                    "diversity_key": None,
                }
            )
    return candidates


def _raw_portrait_source_windows(metadata: dict) -> list[tuple[float, float]]:
    try:
        win_start = float(metadata.get("source_start") or 0.0)
        win_end = float(metadata.get("source_end") or 0.0)
    except (TypeError, ValueError):
        return []
    if win_end - win_start <= 0.08:
        return []
    return [(win_start, win_end)]


def _segment_payload(index: int, seg, *, recent_template_ids: set[str]) -> dict:
    start_sec = round(seg.timeline_start_frame / TIMELINE_FPS, 3)
    end_sec = round(seg.timeline_end_frame / TIMELINE_FPS, 3)
    source_start = round(seg.source_start_frame / TIMELINE_FPS, 3)
    source_end = round(seg.source_end_frame / TIMELINE_FPS, 3)
    _, separator, clip_id = str(seg.window_id or "").partition(":")
    is_opening = index == 0 or str(seg.phase or "").strip().lower() == "opening"
    slot_phase = "portrait_opening" if is_opening else "portrait_main"
    return {
        "segment_id": f"portrait_{index + 1}",
        "asset_id": seg.template_id or None,
        "clip_id": clip_id if separator and clip_id else None,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "source_start": source_start,
        "source_end": source_end,
        "role": seg.role or "main",
        "source_mode": seg.source_mode,
        "boundary_source": seg.boundary_source,
        "boundary_reason": seg.boundary_reason,
        "unit_ids": list(seg.unit_ids),
        "slot_phase": slot_phase,
        "recently_used_material": (seg.template_id or "") in recent_template_ids,
        "timeline_start_frame": seg.timeline_start_frame,
        "timeline_end_frame": seg.timeline_end_frame,
        "source_start_frame": seg.source_start_frame,
        "source_end_frame": seg.source_end_frame,
    }


def _portrait_windows(segments) -> list[dict]:
    return [
        {
            "window_id": f"pwin_{index:03d}",
            "start_frame": seg.timeline_start_frame,
            "end_frame": seg.timeline_end_frame,
            "unit_ids": list(seg.unit_ids),
            "boundary_source": seg.boundary_source,
            "phase": seg.phase,
        }
        for index, seg in enumerate(segments)
    ]


def _broll_windows(broll_slots: list[dict], portrait_windows: list[dict]) -> list[dict]:
    windows: list[dict] = []
    portrait_cut_frames = _portrait_cut_frames(portrait_windows)
    for index, slot in enumerate(s for s in broll_slots if isinstance(s, dict)):
        start_frame = int(slot.get("start_frame", 0) or 0)
        end_frame = int(slot.get("end_frame", 0) or 0)
        placement = legalize_broll_window_frames(
            start_frame=start_frame,
            end_frame=end_frame,
            fps=TIMELINE_FPS,
            portrait_cut_frames=portrait_cut_frames,
        )
        if placement is None:
            continue
        start_frame = placement.start_frame
        end_frame = placement.end_frame
        host_portrait_window_ids = [
            str(window.get("window_id") or "")
            for window in portrait_windows
            if start_frame < int(window.get("end_frame", 0) or 0)
            and end_frame > int(window.get("start_frame", 0) or 0)
        ]
        windows.append(
            {
                "window_id": f"bwin_{index:03d}",
                "start_frame": start_frame,
                "end_frame": end_frame,
                "length_frames": max(0, end_frame - start_frame),
                "source_length_frames": placement.source_length_frames,
                "pad_start": placement.pad_start,
                "pad_end": placement.pad_end,
                "host_unit_ids": list(slot.get("unit_ids") or []),
                "host_portrait_window_ids": host_portrait_window_ids,
                "text": str(slot.get("text") or ""),
                "boundary_source": slot.get("boundary_source") or "narration_unit",
            }
        )
    return windows


def _portrait_cut_frames(portrait_windows: list[dict]) -> list[int]:
    return sorted(
        {
            int(frame)
            for window in portrait_windows
            for frame in (window.get("start_frame"), window.get("end_frame"))
            if frame is not None
        }
    )
