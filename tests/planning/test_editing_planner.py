"""Unit tests for the PURE deterministic editing-agent boundary/timeline planner.

Proves the CRITICAL INVARIANTS and core algorithm:
  (a) frame_grid: frame_index(t) == floor(t*fps + 0.5), including the exact .5 case
      where it DIFFERS from round() (banker's rounding); adjacent windows are exactly
      contiguous, each B-A frames, with zero overlap and no duplicated junction frame;
      source slices are exactly the timeline window length;
  (b) beam search picks the expected boundary windows on a fixture narration;
  (c) capacity split + backtracking rescue on a constrained fixture (greedy beam
      fails, the rescue backtracks to a feasible packing);
  (d) semantic-only fallback when no audio pauses are supplied, and the audio-pause
      path is exercised (and feasible) when pauses ARE supplied.
"""

from __future__ import annotations

import math

import pytest

from packages.planning.editing import (
    BoundaryConstraints,
    SpokenSegment,
    build_boundary_locked_chunks,
    build_narration_units,
    build_narration_units_from_asr,
    frame_index,
    plan_boundary_timeline,
    quantize_boundary,
    slice_source_window,
    slice_windows,
    to_seconds,
)
from packages.planning.editing.frame_grid import TIMELINE_FPS
from packages.planning.editing.packing import assign_boundary_windows_for_chunks


# ---------------------------------------------------------------------------
# (a) frame grid — single source of truth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t", [0.0, 0.1, 0.5, 1.0, 12.96, 12.9667, 1.0 / 3.0, 7.5, 50.0])
def test_frame_index_is_floor_half_up(t: float) -> None:
    assert frame_index(t) == int(math.floor(t * TIMELINE_FPS + 0.5))


def test_frame_index_half_boundary_differs_from_round() -> None:
    # t chosen so t*30 == 12.5 exactly: floor(.+0.5) -> 13, banker's round() -> 12.
    t = 12.5 / TIMELINE_FPS
    assert t * TIMELINE_FPS == pytest.approx(12.5)
    assert frame_index(t) == 13
    assert round(t * TIMELINE_FPS) == 12
    assert frame_index(t) != round(t * TIMELINE_FPS)


def test_frame_index_never_negative() -> None:
    assert frame_index(-5.0) == 0


def test_quantize_boundary_round_trips_to_grid() -> None:
    for k in range(0, 200):
        t = k / TIMELINE_FPS
        assert frame_index(quantize_boundary(t)) == k
        assert to_seconds(k) == pytest.approx(k / TIMELINE_FPS)


def test_slice_windows_are_contiguous_exact_no_overlap() -> None:
    # Boundaries deliberately off-grid so quantization is actually exercised.
    boundaries = [0.0, 1.04, 2.96, 4.0, 7.51]
    windows = slice_windows(boundaries)
    assert len(windows) == 4
    for prev, nxt in zip(windows, windows[1:]):
        # the junction frame is shared: prev ends where next starts (no overlap, no gap)
        assert prev.end_frame == nxt.start_frame
    for window in windows:
        # each window is exactly B - A frames
        assert window.length_frames == window.end_frame - window.start_frame
        assert window.length_frames >= 1
    # each window's frame span equals the quantized boundary span
    frames = [frame_index(b) for b in boundaries]
    for window, (a, b) in zip(windows, zip(frames, frames[1:])):
        assert window.start_frame == a
        assert window.end_frame == b


def test_slice_windows_drops_subframe_window_preserving_total() -> None:
    # 1.0 -> 1.01 is < 1 frame apart; the degenerate window is dropped and folded.
    windows = slice_windows([0.0, 1.0, 1.01, 2.0])
    assert len(windows) == 2
    assert windows[0].start_frame == 0 and windows[0].end_frame == 30
    assert windows[1].start_frame == 30 and windows[1].end_frame == 60
    assert windows[-1].end_frame == frame_index(2.0)


def test_slice_source_window_exact_length() -> None:
    window, pad = slice_source_window(source_start_seconds=2.0, length_frames=45)
    assert window.length_frames == 45
    assert pad == 0
    assert window.start_frame == frame_index(2.0)


def test_slice_source_window_shifts_within_headroom() -> None:
    # source window [1.0, 3.0] = frames [30, 90]; start at 2.5s (frame 75) want 30 frames
    # would overrun (75+30=105 > 90) -> shift start back into headroom, no pad.
    window, pad = slice_source_window(
        source_start_seconds=2.5,
        length_frames=30,
        source_window_start_seconds=1.0,
        source_window_end_seconds=3.0,
    )
    assert window.length_frames == 30
    assert pad == 0
    assert window.end_frame <= frame_index(3.0)


def test_slice_source_window_pads_when_headroom_exhausted() -> None:
    # window [0.0, 1.0] = [0, 30]; start 0, want 45 frames -> only 30 real, pad 15.
    window, pad = slice_source_window(
        source_start_seconds=0.0,
        length_frames=45,
        source_window_start_seconds=0.0,
        source_window_end_seconds=1.0,
    )
    assert window.length_frames == 30
    assert pad == 15
    assert window.length_frames + pad == 45


# ---------------------------------------------------------------------------
# (b) beam search picks the expected boundary windows
# ---------------------------------------------------------------------------


def _portrait_window(window_id: str, template_id: str, duration: float, **kw):
    return {
        "window_id": window_id,
        "template_id": template_id,
        "duration": duration,
        "role": kw.get("role", "main"),
        "confidence": kw.get("confidence", 0.8),
        "start": 0.0,
        "end": duration,
        **kw,
    }


def test_beam_picks_expected_boundaries_on_fixture() -> None:
    units = build_narration_units(
        script="先讲解打磨工艺的细节。再展示补漆效果对比。最后欢迎大家点击咨询预约。",
        asr_segments=None,
        video_duration=20.0,
    )
    # A high-confidence hook for the opening, two mains for the rest.
    cands = [
        _portrait_window("w_hook", "HOOK", 10.0, role="hook", confidence=0.95),
        _portrait_window("w_main1", "MAIN1", 10.0, role="main", confidence=0.85),
        _portrait_window("w_main2", "MAIN2", 10.0, role="main", confidence=0.80),
    ]
    plan = plan_boundary_timeline(
        narration_units=units,
        portrait_candidates=cands,
        constraints=BoundaryConstraints(target_duration=20.0),
    )
    assert plan.ok
    # opening slot prefers the hook-role window (role bonus dominates in phase=opening)
    assert plan.segments[0].template_id == "HOOK"
    assert plan.segments[0].phase == "opening"
    # no adjacent template repeats (adjacency penalty pushes diversity)
    templates = [s.template_id for s in plan.segments]
    assert all(a != b for a, b in zip(templates, templates[1:]))
    # timeline fully contiguous + frame-exact + total frames match target
    _assert_contiguous_frame_exact(plan)
    assert plan.total_frames == frame_index(20.0)


def test_beam_full_coverage_no_overextension() -> None:
    units = build_narration_units(
        script="第一段讲产品卖点。第二段讲使用方法。第三段讲优惠活动。",
        asr_segments=None,
        video_duration=18.0,
    )
    cands = [_portrait_window(f"w{i}", f"T{i}", 12.0, confidence=0.5 + i * 0.1) for i in range(4)]
    plan = plan_boundary_timeline(
        narration_units=units,
        portrait_candidates=cands,
        constraints=BoundaryConstraints(target_duration=18.0),
    )
    assert plan.ok
    for seg in plan.segments:
        # source slice never longer than the assigned window's capacity
        assert seg.source_end_frame - seg.source_start_frame == seg.timeline_length_frames


# ---------------------------------------------------------------------------
# (c) capacity split + backtracking rescue
# ---------------------------------------------------------------------------


def test_capacity_cap_keeps_chunks_within_cap() -> None:
    segs = [SpokenSegment(start=i * 3.0, end=(i + 1) * 3.0, text=f"第{i + 1}句话内容讲解。") for i in range(8)]
    units = build_narration_units_from_asr(segs, 24.0)
    cands = [_portrait_window(c, c.upper(), 7.0, confidence=0.9) for c in "abcd"]
    plan = plan_boundary_timeline(
        narration_units=units,
        portrait_candidates=cands,
        constraints=BoundaryConstraints(target_duration=24.0, max_chunk_duration=6.0),
    )
    assert plan.ok
    for seg in plan.segments:
        # +1 frame slack for the grid boundary
        assert seg.timeline_length_frames <= 6 * TIMELINE_FPS + 1
    _assert_contiguous_frame_exact(plan)


def test_backtracking_rescue_finds_feasible_packing_when_greedy_beam_fails() -> None:
    # Greedy (beam_width=1) assigns the high-confidence BIG window to chunk 1 and then
    # cannot cover the 9s tail; the rescue backtracks to S1/S2/BIG.
    chunks = [
        {"start": 0.0, "end": 6.0, "duration": 6.0, "phase": "opening", "unit_ids": []},
        {"start": 6.0, "end": 12.0, "duration": 6.0, "phase": "main", "unit_ids": []},
        {"start": 12.0, "end": 21.0, "duration": 9.0, "phase": "tail", "unit_ids": []},
    ]
    cands = [
        _portrait_window("big", "BIG", 10.0, confidence=5.0),
        _portrait_window("s1", "S1", 6.0, confidence=0.1),
        _portrait_window("s2", "S2", 6.0, confidence=0.1),
    ]
    plan, trace, _score = assign_boundary_windows_for_chunks(
        chunks=chunks,
        portrait_candidates=cands,
        target_duration=21.0,
        variant="v",
        candidate_scope="all",
        relax_passes=[{"allow_adjacent": True, "max_uses": 1, "allow_original": True}],
        beam_width=1,
    )
    assert plan is not None, "the backtracking rescue should find a feasible packing"
    assert any(row.get("status") == "rescued" for row in trace)
    # the 9s tail must be covered by the only window large enough (BIG)
    assert plan[-1]["template_id"] == "BIG"
    assert round(plan[-1]["duration"], 2) == 9.0


def test_infeasible_capacity_returns_no_plan_not_overextension() -> None:
    # 4 chunks of 6s = 24s, but three 9s windows can each cover only one 6s chunk
    # (3s left can't cover another 6s chunk) -> only 18s coverable -> honest failure.
    segs = [SpokenSegment(start=i * 3.0, end=(i + 1) * 3.0, text=f"第{i + 1}句话内容讲解。") for i in range(8)]
    units = build_narration_units_from_asr(segs, 24.0)
    cands = [_portrait_window(c, c.upper(), 9.0, confidence=0.9) for c in "abc"]
    plan = plan_boundary_timeline(
        narration_units=units,
        portrait_candidates=cands,
        constraints=BoundaryConstraints(target_duration=24.0),
    )
    assert not plan.ok
    assert plan.segments == []


# ---------------------------------------------------------------------------
# (d) semantic-only fallback vs. audio-pause path
# ---------------------------------------------------------------------------


def test_semantic_only_fallback_when_no_audio_pauses() -> None:
    units = build_narration_units(
        script="先讲解打磨工艺的细节非常重要。再展示补漆效果对比清晰可见。最后欢迎点击咨询预约下单。",
        asr_segments=None,
        video_duration=24.0,
    )
    cands = [_portrait_window(f"w{i}", f"T{i}", 12.0, confidence=0.8) for i in range(4)]
    plan = plan_boundary_timeline(
        narration_units=units,
        portrait_candidates=cands,
        constraints=BoundaryConstraints(target_duration=24.0),
    )
    assert plan.ok
    assert plan.used_audio_pauses is False
    # with no pauses every accepted boundary is a semantic sentence end
    sources = {s.boundary_source for s in plan.segments}
    assert "semantic_audio_pause" not in sources


def test_audio_pause_path_is_used_when_pauses_supplied() -> None:
    units = build_narration_units(
        script="先讲解打磨工艺的细节非常重要。再展示补漆效果对比清晰可见。最后欢迎点击咨询预约下单。",
        asr_segments=None,
        video_duration=24.0,
    )
    pauses = [
        {"start": u.end - 0.02, "end": u.end + 0.16, "duration": 0.18} for u in units[:-1]
    ]
    cands = [_portrait_window(f"w{i}", f"T{i}", 12.0, confidence=0.8) for i in range(4)]
    plan = plan_boundary_timeline(
        narration_units=units,
        portrait_candidates=cands,
        constraints=BoundaryConstraints(target_duration=24.0),
        audio_pauses=pauses,
    )
    assert plan.ok
    assert plan.used_audio_pauses is True
    _assert_contiguous_frame_exact(plan)


def test_semantic_boundaries_match_eligible_sentence_ends() -> None:
    units = build_narration_units(
        script="第一句讲解内容。第二句讲解内容。第三句讲解内容收尾。",
        asr_segments=None,
        video_duration=18.0,
    )
    chunks, _trace = build_boundary_locked_chunks(units, 18.0)
    chunk_ends = {round(c["end"], 3) for c in chunks}
    eligible_ends = {
        round(u.end, 3) for u in units if (u.portrait_cut_allowed or u.hard_end)
    }
    # every interior chunk boundary is one of the eligible sentence ends
    interior = chunk_ends - {round(chunks[-1]["end"], 3)}
    assert interior.issubset(eligible_ends)


def test_fps_mismatch_is_rejected() -> None:
    units = build_narration_units(script="一句话。两句话。", asr_segments=None, video_duration=6.0)
    with pytest.raises(ValueError):
        plan_boundary_timeline(
            narration_units=units,
            portrait_candidates=[_portrait_window("w", "T", 8.0)],
            constraints=BoundaryConstraints(target_duration=6.0),
            fps=25,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _assert_contiguous_frame_exact(plan) -> None:
    for prev, nxt in zip(plan.segments, plan.segments[1:]):
        assert prev.timeline_end_frame == nxt.timeline_start_frame
    for seg in plan.segments:
        assert seg.timeline_length_frames == seg.timeline_end_frame - seg.timeline_start_frame
        assert seg.source_end_frame - seg.source_start_frame == seg.timeline_length_frames
    if plan.segments:
        assert plan.total_frames == plan.segments[-1].timeline_end_frame


def test_quantize_plan_drops_degenerate_subframe_segment_without_misaligning():
    """The single-grid quantizer pairs each segment with its OWN frame window.

    A segment whose timeline span is < 1 frame (here the 2nd: 1.0 -> 1.01s at 30fps,
    frame 30 -> 30) is degenerate. It must be DROPPED gracefully -- not silently
    shift later segments onto the wrong window (the old next(window_iter)+break bug),
    and not hard-crash the render. The surviving segment keeps its correct frame
    window, and the drop is recorded in the plan trace (loud, not silent). This is
    unreachable via the real chunk builder's >0.08s (>=~3 frame) floor; the test pins
    the graceful handling for a future floor change.
    """
    from packages.planning.editing.plan import _quantize_plan

    trace: list = []
    ordered = [
        {"timeline_start": 0.0, "timeline_end": 1.0, "source_start": 0.0},
        {"timeline_start": 1.0, "timeline_end": 1.01, "source_start": 0.0},
    ]
    plan = _quantize_plan(ordered, used_audio_pauses=False, trace=trace)

    # The degenerate 2nd segment is dropped; the survivor keeps its correct window.
    assert len(plan.segments) == 1
    assert plan.segments[0].timeline_start_frame == 0
    assert plan.segments[0].timeline_end_frame == 30
    assert plan.total_frames == 30
    # The drop is recorded loudly in the trace (not silently swallowed).
    assert any(t.get("event") == "degenerate_segment_dropped" for t in trace)
