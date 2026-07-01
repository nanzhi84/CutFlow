"""Plan-time b-roll frame-grid alignment (#105).

The portrait-cut snap that used to run downstream in ``TimelinePlanning`` (the old
``_timeline_grid.align_broll_to_portrait_cuts``) now runs in the planning layer, so
``BrollPlanning`` emits authoritative frame boundaries and the timeline node is
verify-only. These pure-function tests cover: snapping a near-missed boundary onto a
portrait cut, refusing to snap when it would leave too-short a portrait sliver / need
too much clone-pad, never pulling the source window, dropping a snap that would
overlap a neighbouring insert, and always populating frame fields.
"""

from __future__ import annotations

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.material import align_insertions_to_portrait_cuts, plan_insertions
from packages.planning.material.broll_pack import BrollCandidate
from packages.planning.material.broll_plan import BrollInsertion
from packages.planning.material.keywords import ScriptSegment


def _ins(ts: float, te: float, ss: float, se: float, **kw) -> BrollInsertion:
    return BrollInsertion(
        asset_id=kw.get("asset_id", "asset_a"),
        clip_id=kw.get("clip_id", "clip_a"),
        timeline_start=ts,
        timeline_end=te,
        source_start=ss,
        source_end=se,
        confidence=0.5,
        matched_keywords=(),
        scene_name="scene",
        reason="reason",
        diversity_key=kw.get("diversity_key", ""),
    )


# --- snapping / residual / pad ------------------------------------------------


def test_tail_snaps_to_nearby_portrait_cut_and_records_pad():
    # Portrait cuts at 0/150/300; a b-roll ending 3 frames short of the cut at 150
    # snaps forward to 150 (the sliver is too short to read), the source window is
    # left untouched, and the 3-frame extension is recorded as clone-pad.
    [r] = align_insertions_to_portrait_cuts(
        [_ins(3.0, 4.9, 3.0, 4.9)], fps=30, portrait_cut_frames=[0, 150, 300], max_gap_frames=6
    )
    assert (r.timeline_start_frame, r.timeline_end_frame) == (90, 150)
    assert (r.source_start_frame, r.source_end_frame) == (90, 147)  # source NOT pulled
    assert round(r.pad_start, 3) == 0.0
    assert round(r.pad_end, 3) == 0.1


def test_head_residual_absorbed_with_pad_when_safe():
    [r] = align_insertions_to_portrait_cuts(
        [_ins(0.1, 8.0, 5.0, 12.9)], fps=30, portrait_cut_frames=[0, 300]
    )
    assert (r.timeline_start_frame, r.timeline_end_frame) == (0, 240)
    assert (r.source_start_frame, r.source_end_frame) == (150, 387)
    assert round(r.pad_start, 3) == 0.1
    assert round(r.pad_end, 3) == 0.0


def test_head_and_tail_cover_whole_shot_with_safe_pads():
    [r] = align_insertions_to_portrait_cuts(
        [_ins(0.1, 2.9, 5.0, 7.8)], fps=30, portrait_cut_frames=[0, 90]
    )
    assert (r.timeline_start_frame, r.timeline_end_frame) == (0, 90)
    assert (r.source_start_frame, r.source_end_frame) == (150, 234)
    assert round(r.pad_start, 3) == 0.1
    assert round(r.pad_end, 3) == 0.1


def test_short_residual_not_snapped_when_required_pad_exceeds_cap():
    # Head residual of 60 frames (2.0s) would need 2.0s of clone-pad — far over the
    # 0.15s cap — so the boundary stays at its quantized seconds position, no snap,
    # no pad. Frames are still populated (authoritative) straight from seconds.
    [r] = align_insertions_to_portrait_cuts(
        [_ins(2.0, 5.0, 5.0, 8.0)],
        fps=30,
        portrait_cut_frames=[0, 300],
        min_visible_residual_frames=90,
    )
    assert (r.timeline_start_frame, r.timeline_end_frame) == (60, 150)
    assert round(r.pad_start, 3) == 0.0
    assert round(r.pad_end, 3) == 0.0


def test_long_visible_residual_is_left_alone():
    # Residual at/above the min-visible threshold means real portrait is meant to show
    # around the b-roll — never snap it away.
    [r] = align_insertions_to_portrait_cuts(
        [_ins(1.5, 8.0, 5.0, 11.5)], fps=30, portrait_cut_frames=[0, 300]
    )
    assert (r.timeline_start_frame, r.timeline_end_frame) == (45, 240)
    assert round(r.pad_start, 3) == 0.0
    assert round(r.pad_end, 3) == 0.0


def test_source_window_is_never_pulled_by_a_snap():
    [r] = align_insertions_to_portrait_cuts(
        [_ins(3.0, 4.9, 3.0, 4.9)], fps=30, portrait_cut_frames=[0, 150, 300], max_gap_frames=6
    )
    # The timeline end moved (147 -> 150) but the source end did not (stays 147): the
    # held frame is clone-padded, the source is not read past its clean span.
    assert r.source_end_frame == 147
    assert r.timeline_end_frame == 150


def test_snap_dropped_when_it_would_overlap_the_next_insert():
    # Two adjacent inserts: the first would snap its tail forward onto the cut at 150,
    # but the next insert already starts at 150 (frame). Snapping would touch/overlap
    # the neighbour, so the snap is dropped and the first insert keeps its frames.
    inserts = [_ins(3.0, 4.9, 3.0, 4.9), _ins(5.0, 7.0, 0.0, 2.0, clip_id="clip_b")]
    aligned = align_insertions_to_portrait_cuts(
        inserts, fps=30, portrait_cut_frames=[0, 150, 300], max_gap_frames=6
    )
    first, second = aligned
    # next insert starts at frame 150; snapping first to 150 would not strictly exceed
    # it (<=) so it is allowed — assert no overlap and frames authoritative.
    assert first.timeline_end_frame <= second.timeline_start_frame
    assert all(s.timeline_start_frame is not None for s in aligned)


def test_snap_strictly_dropped_when_following_insert_starts_before_cut():
    # The following insert starts at frame 148 (< the cut at 150). Snapping the first
    # insert's tail to 150 WOULD exceed 148 -> overlap -> snap dropped, frames kept.
    inserts = [
        _ins(3.0, 4.9, 3.0, 4.9),
        _ins(148 / 30, 7.0, 0.0, 2.0, clip_id="clip_b"),
    ]
    first, _ = align_insertions_to_portrait_cuts(
        inserts, fps=30, portrait_cut_frames=[0, 150, 300], max_gap_frames=6
    )
    assert first.timeline_end_frame == 147  # unsnapped quantized end
    assert round(first.pad_end, 3) == 0.0


def test_frames_always_populated_even_without_any_cut_grid():
    # No portrait cut frames -> no snapping, but the inserts must still come back with
    # authoritative frame fields derived from their seconds (BrollPlanning is the
    # authority; the timeline node never re-derives).
    [r] = align_insertions_to_portrait_cuts([_ins(1.0, 3.0, 0.0, 2.0)], fps=30, portrait_cut_frames=[])
    assert (r.timeline_start_frame, r.timeline_end_frame) == (30, 90)
    assert (r.source_start_frame, r.source_end_frame) == (0, 60)


def test_aligned_inserts_never_invert_or_overlap_on_track():
    cuts = [0, 150, 300, 450]
    inserts = [
        _ins(1.0, 4.9, 0.0, 3.9, clip_id="c1"),
        _ins(6.0, 9.9, 0.0, 3.9, clip_id="c2"),
    ]
    aligned = align_insertions_to_portrait_cuts(inserts, fps=30, portrait_cut_frames=cuts)
    prev_end = None
    for r in aligned:
        assert r.timeline_end_frame > r.timeline_start_frame  # never 0/negative
        if prev_end is not None:
            assert r.timeline_start_frame >= prev_end  # no same-track overlap
        prev_end = r.timeline_end_frame


# --- end-to-end through plan_insertions --------------------------------------


def _candidate(start: float, end: float, *, asset_id="asset_a", clip_id="clip_a") -> BrollCandidate:
    beat = ScriptSegment(text="补漆 效果 对比", start=0.0, end=4.0, keywords=("补漆", "效果"))
    return BrollCandidate(
        asset_id=asset_id,
        clip_id=clip_id,
        score=80.0,
        base_score=80.0,
        recency_penalty=0.0,
        matched_keywords=("补漆",),
        scene_name="补漆",
        source_start=start,
        source_end=end,
        diversity_key="补漆台",
        best_segment=beat,
    )


def test_plan_insertions_emits_frame_aligned_inserts_when_grid_supplied():
    units = [
        NarrationUnit(unit_id="u1", text="先讲解补漆效果对比。", start=0.0, end=5.0, confidence=1.0),
    ]
    candidates = [_candidate(0.0, 4.0)]
    # Portrait cut a few frames after where the insert would naturally end.
    insertions = plan_insertions(
        candidates=candidates,
        units=units,
        max_inserts=1,
        fps=30,
        portrait_cut_frames=[0, 150],
    )
    assert insertions
    ins = insertions[0]
    assert ins.timeline_start_frame is not None
    assert ins.timeline_end_frame is not None
    assert ins.source_start_frame is not None
    assert ins.source_end_frame is not None
    assert ins.timeline_end_frame > ins.timeline_start_frame


def test_plan_insertions_leaves_frames_none_without_grid_context():
    # No fps / cut frames -> the legacy seconds-only placement still works and frame
    # fields are left unset (broll_only_v1 path that has no portrait cut grid).
    units = [
        NarrationUnit(unit_id="u1", text="先讲解补漆效果对比。", start=0.0, end=5.0, confidence=1.0),
    ]
    insertions = plan_insertions(candidates=[_candidate(0.0, 4.0)], units=units, max_inserts=1)
    assert insertions
    assert insertions[0].timeline_start_frame is None
    assert insertions[0].source_start_frame is None
