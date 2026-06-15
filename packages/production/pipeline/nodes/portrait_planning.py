"""PortraitPlanning node: the real boundary/timeline portrait plan.

Thin wiring around the PURE planner in :mod:`packages.planning.editing`. It feeds the
planner three honest inputs and emits the frame-contiguous portrait plan the render
nodes consume — no seeded/placeholder timeline:

  - narration units: re-derived through the editing-agent splitter so each unit
    carries the boundary fields (``portrait_cut_allowed`` / ``hard_end`` /
    ``boundary_score``) the boundary builder needs, while keeping the real aligned
    timing from the NarrationAlignment artifact;
  - portrait source-window candidates: each usable portrait material candidate's
    real source span ``[0, source_duration]`` (ranked by the material pack);
  - audio pauses: detected by running ffmpeg ``silencedetect`` on the produced TTS
    audio. With real TTS this finds real 气口 and cuts snap into silences; with the
    sandbox 440Hz tone it finds (near) none, so the planner falls back to
    semantic-only boundaries. Pauses are NEVER fabricated.

When the candidates cannot capacity-cover the audio the planner returns no segments
and we soft-degrade honestly via ``material.insufficient.portrait`` — never a
fabricated plan.
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.contracts.artifacts import PortraitPlanArtifact
from packages.media.audio import detect_silence_windows
from packages.planning.editing import (
    TIMELINE_FPS,
    BoundaryConstraints,
    SpokenSegment,
    build_narration_units,
    plan_boundary_timeline,
)
from packages.planning.selection.recency_context import (
    build_portrait_recency_context_from_ledger,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration = state.require(ArtifactKind.narration_units).payload or {}
    raw_units = narration.get("units", []) or []
    duration = max([float(unit.get("end", 0)) for unit in raw_units] or [1.0])

    hard_fail = state.request.strictness.portrait_insufficient_policy == "hard_fail"
    portrait_candidate_ids = [
        item.get("asset_id")
        for item in material.get("portrait_candidates", [])
        if item.get("asset_id")
    ]
    if hard_fail and not portrait_candidate_ids:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "Portrait main track cannot cover the full audio.",
        )

    # Build planner candidates from the ranked material pack. Each candidate is the
    # real source span [0, source_duration]; the planner enforces coverage/capacity.
    # Recency context (weighted recency + opening guard, §6.6/§31/§32.10) is attached
    # so the already-ported scoring (is_recent_portrait_candidate / opening penalty /
    # exact-vs-similar split) fires on the real production path instead of dead-defaulting.
    portrait_ledger = ctx.repository.recent_selections(
        case_id=state.request.case_id, medium="portrait"
    )
    candidates = _portrait_window_candidates(ctx, portrait_candidate_ids, portrait_ledger)
    if portrait_candidate_ids and not candidates:
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "Portrait source window cannot cover the full audio.",
        )

    # Re-derive narration units through the editing splitter so boundary fields are
    # populated, keeping the real aligned timing as the spoken-segment skeleton.
    spoken = [
        SpokenSegment(
            start=float(unit.get("start", 0.0)),
            end=float(unit.get("end", 0.0)),
            text=str(unit.get("text") or ""),
        )
        for unit in raw_units
        if str(unit.get("text") or "").strip()
    ]
    planner_units = build_narration_units(
        script=state.request.script,
        asr_segments=spoken or None,
        video_duration=duration,
    )

    # Detect real audio pauses on the produced TTS audio (semantic-only fallback when
    # the audio is the sandbox tone and has no reliable silences).
    audio_pauses = _detect_audio_pauses(ctx)

    plan, escalation = _plan_with_escalation(
        narration_units=planner_units,
        candidates=candidates,
        duration=duration,
        audio_pauses=audio_pauses or None,
    )
    if not plan.ok:
        # Honest hard-fail: even after the escalation ladder (full-pool single pass +
        # capacity-controlled split retry) the candidates cannot cover the audio. This
        # is a true-yield failure, never a silent degrade or a fabricated plan.
        raise NodeExecutionError(
            ErrorCode.material_insufficient_portrait,
            "Portrait candidates cannot cover the full audio without over-extension.",
        )

    recent_template_ids = {
        str(c.get("template_id") or "")
        for c in candidates
        if isinstance(c.get("recent_usage"), dict) and c["recent_usage"].get("is_recently_used")
    }
    segments = [
        _segment_payload(index, seg, recent_template_ids=recent_template_ids, total=len(plan.segments))
        for index, seg in enumerate(plan.segments)
    ]
    total_duration = round(plan.total_frames / TIMELINE_FPS, 3)
    payload = PortraitPlanArtifact(
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
            "recently_used_segment_count": sum(
                1 for seg in segments if seg.get("recently_used_material")
            ),
        },
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[ctx.artifact(ArtifactKind.plan_portrait, payload, "PortraitPlanArtifact.v1")]
    )


def _plan_with_escalation(
    *,
    narration_units,
    candidates: list[dict],
    duration: float,
    audio_pauses,
):
    """Drive the portrait-insufficiency escalation ladder before giving up.

    The single (default) pass already reaches the unlimited-reuse fallback scope.
    When even that fails to cover the audio, this runs the OLD ladder's true-yield
    recovery rounds that do NOT need an external material-expansion service:

      1. ``full_pool`` — re-plan against the full candidate pool (in genesis the node
         already receives the full ranked pool, so this is the recorded baseline pass);
      2. ``capacity_controlled_split`` — re-plan with
         ``max_chunk_duration=longest_usable_source_window`` and
         ``include_unlimited_reuse_scope=False``, so over-long chunks are split below
         the longest available source window (letting shorter windows participate)
         while banning the infinite-reuse crutch.

    Returns ``(plan, escalation_diagnostics)``. Still hard-fails upstream (plan.ok
    False) when no round can cover — never a fabricated or silently-degraded plan.
    """
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
        }

    # Capacity-controlled split retry: shorten over-long chunks to the longest usable
    # source window so shorter windows can cover them; ban unlimited reuse so this is a
    # real coverage recovery, not the over-extension crutch.
    if longest_usable > 0.08:
        split_plan = plan_boundary_timeline(
            narration_units=narration_units,
            portrait_candidates=candidates,
            constraints=BoundaryConstraints(
                target_duration=duration,
                max_chunk_duration=round(longest_usable, 3),
                include_unlimited_reuse_scope=False,
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
            }

    return plan, {
        "stage": "exhausted",
        "attempts": attempts,
        "capacity_controlled_split": False,
        "longest_usable_source_window": round(longest_usable, 3),
    }


def _portrait_window_candidates(ctx: NodeContext, asset_ids: list[str], ledger) -> list[dict]:
    """One source-window candidate per usable portrait asset (ranked order kept).

    ``window_id`` / ``template_id`` are the asset id so the planned segment's
    ``template_id`` maps straight back to the material asset for the render node.

    Each candidate also carries a ``recent_usage`` context built from the case's
    recent portrait ledger rows (most-recent-first), so the ported scoring side
    actually demotes a recently-used template and blocks a consecutive opening reuse
    — without it ``is_recent_portrait_candidate`` defaulted to False for everyone.
    """
    candidates: list[dict] = []
    for rank, asset_id in enumerate(asset_ids):
        source = ctx.source_artifact_for_asset(asset_id)
        source_duration = (
            float(source.media_info.duration_sec or 0)
            if source and source.media_info
            else 0.0
        )
        if source_duration <= 0.08:
            continue
        recent_usage = build_portrait_recency_context_from_ledger(
            entries=ledger,
            template_id=asset_id,
            diversity_key=None,
        )
        candidates.append(
            {
                "window_id": asset_id,
                "template_id": asset_id,
                "template_name": asset_id,
                "start": 0.0,
                "end": round(source_duration, 3),
                "duration": round(source_duration, 3),
                "role": "main",
                # Material pack ranks by score desc; turn rank into a stable
                # confidence so the highest-ranked usable asset wins ties.
                "confidence": round(max(0.1, 0.9 - rank * 0.05), 3),
                "source_mode_hint": "lipsynced",
                "recent_usage": recent_usage,
                "recency_penalty": recent_usage.get("recency_penalty", 0.0),
                "diversity_key": None,
            }
        )
    return candidates


def _detect_audio_pauses(ctx: NodeContext) -> list[dict]:
    audio = ctx.state.artifacts.get(ArtifactKind.audio_tts)
    if audio is None or not audio.uri:
        return []
    try:
        audio_path = ctx.artifact_path(audio)
    except NodeExecutionError:
        return []
    return detect_silence_windows(audio_path)


def _segment_payload(index: int, seg, *, recent_template_ids: set[str], total: int) -> dict:
    start_sec = round(seg.timeline_start_frame / TIMELINE_FPS, 3)
    end_sec = round(seg.timeline_end_frame / TIMELINE_FPS, 3)
    source_start = round(seg.source_start_frame / TIMELINE_FPS, 3)
    source_end = round(seg.source_end_frame / TIMELINE_FPS, 3)
    # Opening guard: the first portrait segment is the run's opening; recorded distinctly
    # as ``portrait_opening`` so the next run's recency context can apply the opening
    # penalty (no-consecutive-opening-reuse). The planner phase label may already say
    # "opening" for the first chunk — honour either signal.
    is_opening = index == 0 or str(seg.phase or "").strip().lower() == "opening"
    slot_phase = "portrait_opening" if is_opening else "portrait_main"
    return {
        "segment_id": f"portrait_{index + 1}",
        "asset_id": seg.template_id or None,
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
