"""Unit tests for the deterministic huazi layout box generator (pure logic)."""

from __future__ import annotations

import copy

from packages.production.pipeline._huazi_layout import generate_layout_boxes

_RES = (1920, 1080)
# 10 full-width chars: drops the compact tier and, with a tight safety zone,
# keeps the candidate count below the 24 cap so no truncation masks a filter.
_LONG = "一二三四五六七八九十"


def _boxes(text="限时特惠", *, top_y=0.78, neighbors=None):
    return generate_layout_boxes(
        event_text=text,
        resolution=_RES,
        normal_caption_top_y=top_y,
        neighbor_boxes=neighbors or [],
    )


def _small(neighbors=None):
    # top/upper/middle only (18 boxes, no truncation) so specific boxes are
    # guaranteed present and a neighbor penalty never evicts its own box.
    return _boxes(text=_LONG, top_y=0.55, neighbors=neighbors)


def test_deterministic_snapshot_byte_equal():
    first = _small(neighbors=["middle_center_medium"])
    second = _small(neighbors=["middle_center_medium"])
    assert first == second
    # A fresh call must not share mutable structure with a prior result.
    first[0]["region_tags"].append("mutated")
    assert "mutated" not in second[0]["region_tags"]


def test_candidate_count_within_band_and_capped():
    assert 12 <= len(_boxes()) <= 24
    # A permissive safety zone floods the grid; the cap holds at 24.
    assert len(_boxes(top_y=0.95)) == 24
    # A high safety zone leaves only the top two bands -> a smaller honest set.
    assert len(_boxes(text=_LONG, top_y=0.40)) == 12


def test_required_fields_and_rect_bounds():
    for box in _boxes():
        assert set(box) == {
            "layout_box_id",
            "rect",
            "text_align",
            "max_lines",
            "text_capacity",
            "allowed_enter_directions",
            "collision_score",
            "region_tags",
        }
        rect = box["rect"]
        assert 0.0 <= rect["x"] and rect["x"] + rect["w"] <= 1.0
        assert 0.0 <= rect["y"] and rect["y"] + rect["h"] <= 1.0
        assert box["max_lines"] == 1
        assert box["text_align"] == box["region_tags"][1]
        assert box["region_tags"][1] in {"left", "center", "right"}


def test_safety_zone_drops_lower_band():
    # Baseline: with a low caption zone the lower band fits and is emitted.
    assert "lower" in {b["region_tags"][0] for b in _boxes(text=_LONG, top_y=0.95)}
    # Raising the safety zone removes the intruding lower band but keeps
    # lower_middle, which stays above the limit.
    bands = {b["region_tags"][0] for b in _boxes(text=_LONG, top_y=0.70)}
    assert "lower" not in bands
    assert "lower_middle" in bands


def test_capacity_filter_drops_compact_for_ten_full_width_chars():
    caps = {b["layout_box_id"].rsplit("_", 1)[1] for b in _boxes(text=_LONG)}
    assert "compact" not in caps
    assert {"medium", "wide"} <= caps


def test_neighbor_penalty_raises_collision_score():
    box_id = "middle_center_medium"
    without = {b["layout_box_id"]: b["collision_score"] for b in _small()}
    penalised = {b["layout_box_id"]: b["collision_score"] for b in _small([box_id])}
    assert box_id in without and box_id in penalised
    assert penalised[box_id] == round(without[box_id] + 0.30, 4)
    # Only the named box is penalised.
    other = "upper_left_medium"
    assert penalised[other] == without[other]


def test_sorted_by_ascending_collision_score():
    scores = [b["collision_score"] for b in _boxes()]
    assert scores == sorted(scores)


def test_enter_direction_constraints():
    by_id = {b["layout_box_id"]: b for b in _small()}
    assert by_id["upper_left_medium"]["allowed_enter_directions"] == ["left", "up"]
    assert by_id["upper_right_medium"]["allowed_enter_directions"] == ["right", "up"]
    assert by_id["upper_center_medium"]["allowed_enter_directions"] == ["up"]
    # A top-band center box forbids every slide-in direction (fade/pop only).
    assert by_id["top_center_medium"]["allowed_enter_directions"] == []
    # Top edge anchors still allow their lateral slide.
    assert by_id["top_left_medium"]["allowed_enter_directions"] == ["left", "up"]


def test_input_neighbor_list_not_mutated():
    neighbors = ["middle_center_medium"]
    snapshot = copy.deepcopy(neighbors)
    _boxes(text="限时", neighbors=neighbors)
    assert neighbors == snapshot
