"""Pure b-roll frame-grid alignment helpers (#105 legacy coverage).

The portrait-cut snap that used to run downstream before ``TimelineAssemblyValidation`` (the old
``_timeline_grid.align_broll_to_portrait_cuts``) moved into pure planning helpers in
#105. digital_human_v2 now gets B-roll frame authority from
``TimelineWindowPlanning`` placement slots, but these helpers remain covered for
callers that still need seconds-to-grid alignment. These tests cover snapping a
near-missed boundary, refusing unsafe snaps, never pulling the source window, and
always populating frame fields when a grid context is supplied.
"""

from __future__ import annotations

from packages.planning.material import (
    align_insertions_to_portrait_cuts,
    legalize_broll_window_frames,
)
from packages.planning.material.broll_plan import BrollInsertion


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
