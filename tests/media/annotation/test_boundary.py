from __future__ import annotations

from packages.media.annotation import apply_safety_inset, has_internal_cut, snap_to_cuts


def test_snap_to_cuts_snaps_within_tolerance():
    s, e = snap_to_cuts(2.05, 5.9, [2.0, 6.0], tol=0.25)
    assert s == 2.0
    assert e == 6.0


def test_snap_to_cuts_keeps_value_when_no_cut_in_range():
    s, e = snap_to_cuts(2.05, 5.9, [10.0], tol=0.25)
    assert (s, e) == (2.05, 5.9)


def test_snap_to_cuts_ties_take_earlier_cut():
    # value 5.0 equidistant from 4.8 and 5.2 -> earlier (4.8).
    s, _e = snap_to_cuts(5.0, 9.0, [4.8, 5.2], tol=0.25)
    assert s == 4.8


def test_snap_to_cuts_avoids_collapse():
    # Both ends would snap to 3.0; end reverts to its original to stay non-empty.
    s, e = snap_to_cuts(3.05, 3.1, [3.0], tol=0.25)
    assert e > s


def test_apply_safety_inset_insets_both_ends():
    out = apply_safety_inset(1.0, 2.0, fps=25.0, inset_frames=1)
    assert out is not None
    s, e = out
    assert abs(s - 1.04) < 1e-6
    assert abs(e - 1.96) < 1e-6


def test_apply_safety_inset_drops_too_short():
    assert apply_safety_inset(1.0, 1.05, fps=25.0, inset_frames=2) is None
    assert apply_safety_inset(2.0, 1.0) is None  # already reversed


def test_has_internal_cut_detects_interior_cut():
    assert has_internal_cut(0.0, 5.0, [2.5], edge_guard=0.12) is True


def test_has_internal_cut_ignores_edge_band_cuts():
    assert has_internal_cut(0.0, 5.0, [0.05, 4.95], edge_guard=0.12) is False


def test_has_internal_cut_short_segment_no_middle():
    assert has_internal_cut(1.0, 1.1, [1.05], edge_guard=0.12) is False
