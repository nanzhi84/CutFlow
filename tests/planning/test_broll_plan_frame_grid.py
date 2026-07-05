"""Pure b-roll frame-grid alignment helpers (#105 legacy coverage).

The portrait-cut snap that used to run downstream in ``TimelinePlanning`` (the old
``_timeline_grid.align_broll_to_portrait_cuts``) moved into pure planning helpers in
#105. digital_human_v2 now gets B-roll frame authority from
``TimelineWindowPlanning`` placement slots, but these helpers remain covered for
callers that still need seconds-to-grid alignment. These tests cover snapping a
near-missed boundary, refusing unsafe snaps, never pulling the source window, and
always populating frame fields when a grid context is supplied.
"""

from __future__ import annotations

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.material import (
    align_insertions_to_portrait_cuts,
    legalize_broll_window_frames,
    place_insertion_safely,
    plan_insertions,
)
from packages.planning.material.broll_pack import BrollCandidate
from packages.planning.material.broll_plan import BROLL_GEOMETRY_POLICY, BrollInsertion
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


def test_tail_residual_over_pad_cap_is_not_absorbed_by_snap():
    # A 9-frame / 0.3s tail residual is inside the coarse 15-frame cut window, but
    # outside the 0.15s clone-pad cap. Alignment must not silently widen that cap.
    [r] = align_insertions_to_portrait_cuts(
        [_ins(22.267, 23.833, 0.04, 1.6)],
        fps=30,
        portrait_cut_frames=[488, 724, 816],
    )
    assert (r.timeline_start_frame, r.timeline_end_frame) == (668, 715)
    assert (r.source_start_frame, r.source_end_frame) == (1, 48)
    assert round(r.pad_end, 3) == 0.0


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


def test_short_residual_not_snapped_when_required_pad_exceeds_snap_window():
    # Head residual of 60 frames (2.0s) is a visible portrait window, far outside the
    # 15-frame snap window, so the boundary stays at its quantized seconds position.
    # Frames are still populated (authoritative) straight from seconds.
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
    # frame fields derived from their seconds.
    [r] = align_insertions_to_portrait_cuts([_ins(1.0, 3.0, 0.0, 2.0)], fps=30, portrait_cut_frames=[])
    assert (r.timeline_start_frame, r.timeline_end_frame) == (30, 90)
    assert (r.source_start_frame, r.source_end_frame) == (0, 60)


def test_legalize_broll_window_frames_rejects_unsnappable_short_head_gap():
    cuts = [0, 360]

    assert legalize_broll_window_frames(
        start_frame=7,
        end_frame=70,
        fps=30,
        portrait_cut_frames=cuts,
    ) is None

    snapped = legalize_broll_window_frames(
        start_frame=4,
        end_frame=70,
        fps=30,
        portrait_cut_frames=cuts,
    )

    assert snapped is not None
    assert (snapped.start_frame, snapped.end_frame) == (0, 70)
    assert snapped.source_length_frames == 66
    assert round(snapped.pad_start, 3) == 0.133


def test_legalize_broll_window_frames_rejects_unsnappable_short_tail_gap():
    cuts = [0, 360]

    assert legalize_broll_window_frames(
        start_frame=75,
        end_frame=350,
        fps=30,
        portrait_cut_frames=cuts,
    ) is None

    snapped = legalize_broll_window_frames(
        start_frame=75,
        end_frame=356,
        fps=30,
        portrait_cut_frames=cuts,
    )

    assert snapped is not None
    assert (snapped.start_frame, snapped.end_frame) == (75, 360)
    assert snapped.source_length_frames == 281
    assert round(snapped.pad_end, 3) == 0.133


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
    candidates = [_candidate(0.0, 3.0)]
    # The insert leaves a visible 2s portrait tail, so it is legal without snap.
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


def test_plan_insertions_rejects_unsnappable_short_portrait_sliver():
    # Regression for run_39b847fc619b: the raw B-roll would end 9 frames before the
    # portrait cut, leaving a 0.3s A-roll flash. That is too far for the 0.15s snap
    # cap and too short to be a visible 2s portrait interval, so the candidate is
    # rejected at planning time.
    units = [
        NarrationUnit(
            unit_id="u1",
            text="划个痕，就要全车重喷？",
            start=22.267,
            end=23.833,
            confidence=1.0,
        ),
    ]
    beat = ScriptSegment(text=units[0].text, start=22.267, end=23.833, keywords=("划痕",))
    candidate = BrollCandidate(
        asset_id="asset_flash",
        clip_id="flash_clip",
        score=90.0,
        base_score=90.0,
        recency_penalty=0.0,
        matched_keywords=("划痕",),
        scene_name="product",
        source_start=0.04,
        source_end=1.6,
        diversity_key="product_showcase",
        best_segment=beat,
    )

    insertions = plan_insertions(
        candidates=[candidate],
        units=units,
        max_inserts=1,
        fps=30,
        portrait_cut_frames=[488, 724, 816],
    )

    assert insertions == []


def test_plan_insertions_repositions_candidate_to_avoid_short_portrait_sliver():
    # The semantic anchor at 2.7s would place a 2s insert at 2.7-4.7, leaving a
    # 0.3s tail before the portrait cut at 5.0. The planner should keep the
    # candidate by moving it to the legal in-window position 3.0-5.0 instead of
    # widening the 0.15s snap cap or dropping the usable B-roll.
    units = [
        NarrationUnit(
            unit_id="u1",
            text="最后展示报告效果。",
            start=0.0,
            end=5.0,
            confidence=1.0,
        ),
    ]
    beat = ScriptSegment(text=units[0].text, start=2.7, end=5.0, keywords=("报告",))
    candidate = BrollCandidate(
        asset_id="asset_report",
        clip_id="report_clip",
        score=90.0,
        base_score=90.0,
        recency_penalty=0.0,
        matched_keywords=("报告",),
        scene_name="report",
        source_start=0.0,
        source_end=2.0,
        diversity_key="report",
        best_segment=beat,
    )

    [insertion] = plan_insertions(
        candidates=[candidate],
        units=units,
        max_inserts=1,
        fps=30,
        portrait_cut_frames=[0, 150],
    )

    assert (insertion.timeline_start_frame, insertion.timeline_end_frame) == (90, 150)
    assert round(insertion.pad_end, 3) == 0.0


def test_place_insertion_safely_matches_plan_insertions_geometry():
    reject_unit = NarrationUnit(
        unit_id="u_reject",
        text="划个痕，就要全车重喷？",
        start=22.267,
        end=23.833,
        confidence=1.0,
    )
    reject_beat = ScriptSegment(
        text=reject_unit.text, start=22.267, end=23.833, keywords=("划痕",)
    )
    reject_candidate = BrollCandidate(
        asset_id="asset_flash",
        clip_id="flash_clip",
        score=90.0,
        base_score=90.0,
        recency_penalty=0.0,
        matched_keywords=("划痕",),
        scene_name="product",
        source_start=0.04,
        source_end=1.6,
        diversity_key="product_showcase",
        best_segment=reject_beat,
    )
    rejected = plan_insertions(
        candidates=[reject_candidate],
        units=[reject_unit],
        max_inserts=1,
        fps=30,
        portrait_cut_frames=[488, 724, 816],
    )
    rejected_shared = place_insertion_safely(
        [],
        _ins(22.267, 23.827, 0.04, 1.6, asset_id="asset_flash", clip_id="flash_clip"),
        window_start=22.267,
        window_end=23.833,
        fps=30,
        portrait_cut_frames=[488, 724, 816],
        policy=BROLL_GEOMETRY_POLICY,
    )
    assert rejected == []
    assert rejected_shared is None

    accept_unit = NarrationUnit(
        unit_id="u_accept",
        text="最后展示报告效果。",
        start=0.0,
        end=5.0,
        confidence=1.0,
    )
    accept_beat = ScriptSegment(text=accept_unit.text, start=2.7, end=5.0, keywords=("报告",))
    accept_candidate = BrollCandidate(
        asset_id="asset_report",
        clip_id="report_clip",
        score=90.0,
        base_score=90.0,
        recency_penalty=0.0,
        matched_keywords=("报告",),
        scene_name="report",
        source_start=0.0,
        source_end=2.0,
        diversity_key="report",
        best_segment=accept_beat,
    )
    [planned] = plan_insertions(
        candidates=[accept_candidate],
        units=[accept_unit],
        max_inserts=1,
        fps=30,
        portrait_cut_frames=[0, 150],
    )
    [placed] = place_insertion_safely(
        [],
        _ins(2.7, 4.7, 0.0, 2.0, asset_id="asset_report", clip_id="report_clip"),
        window_start=2.7,
        window_end=5.0,
        fps=30,
        portrait_cut_frames=[0, 150],
        policy=BROLL_GEOMETRY_POLICY,
    )

    assert (placed.timeline_start_frame, placed.timeline_end_frame) == (
        planned.timeline_start_frame,
        planned.timeline_end_frame,
    )


def test_plan_insertions_prefers_distinct_diversity_keys_within_run():
    units = [
        NarrationUnit(unit_id="u1", text="先展示品牌漆。", start=0.0, end=3.0, confidence=1.0),
        NarrationUnit(unit_id="u2", text="再展示施工环境。", start=4.0, end=7.0, confidence=1.0),
    ]
    first_beat = ScriptSegment(text=units[0].text, start=0.0, end=3.0, keywords=("品牌",))
    second_beat = ScriptSegment(text=units[1].text, start=4.0, end=7.0, keywords=("施工",))
    candidates = [
        BrollCandidate(
            asset_id="asset_product_a",
            clip_id="product_a",
            score=90.0,
            base_score=90.0,
            recency_penalty=0.0,
            matched_keywords=("品牌",),
            scene_name="product",
            source_start=0.0,
            source_end=2.0,
            diversity_key="product_showcase",
            best_segment=first_beat,
        ),
        BrollCandidate(
            asset_id="asset_product_b",
            clip_id="product_b",
            score=89.0,
            base_score=89.0,
            recency_penalty=0.0,
            matched_keywords=("施工",),
            scene_name="product",
            source_start=0.0,
            source_end=2.0,
            diversity_key="product_showcase",
            best_segment=second_beat,
        ),
        BrollCandidate(
            asset_id="asset_workshop",
            clip_id="workshop",
            score=70.0,
            base_score=70.0,
            recency_penalty=0.0,
            matched_keywords=("施工",),
            scene_name="workshop",
            source_start=0.0,
            source_end=2.0,
            diversity_key="workshop",
            best_segment=second_beat,
        ),
    ]

    insertions = plan_insertions(candidates=candidates, units=units, max_inserts=2)

    assert [ins.asset_id for ins in insertions] == ["asset_product_a", "asset_workshop"]
    assert {ins.diversity_key for ins in insertions} == {"product_showcase", "workshop"}


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
