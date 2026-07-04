"""TimelineWindowPlanning node: compile authoritative editing windows."""

from __future__ import annotations

from dataclasses import asdict

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.contracts.artifacts import PortraitPlanArtifact, TimelineWindowsPlan
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing import (
    TIMELINE_FPS,
    BoundaryConstraints,
    plan_boundary_timeline,
)
from packages.planning.material import BROLL_GEOMETRY_POLICY, clean_portrait_source_windows
from packages.production.pipeline._narration_units import build_planner_narration_units
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    boundary = state.require(ArtifactKind.plan_narration_boundary).payload or {}
    raw_units = narration.get("units", []) or []
    duration = max([float(unit.get("end", 0)) for unit in raw_units] or [1.0])

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

    planner_units = build_planner_narration_units(
        raw_units=raw_units,
        source=str(narration.get("source") or ""),
        script=state.request.script,
        duration=duration,
    )
    audio_pauses = boundary.get("pause_windows", []) or []

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
            ),
            ctx.artifact(
                ArtifactKind.plan_portrait,
                portrait_plan_payload,
                "PortraitPlanArtifact.v1",
            ),
        ]
    )


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
    for index, slot in enumerate(s for s in broll_slots if isinstance(s, dict)):
        start_frame = int(slot.get("start_frame", 0) or 0)
        end_frame = int(slot.get("end_frame", 0) or 0)
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
                "host_unit_ids": list(slot.get("unit_ids") or []),
                "host_portrait_window_ids": host_portrait_window_ids,
                "text": str(slot.get("text") or ""),
                "boundary_source": slot.get("boundary_source") or "narration_unit",
            }
        )
    return windows
