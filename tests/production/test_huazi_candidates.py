"""Unit tests for huazi candidate derivation + HuaziPlanningSubagent helpers.

Pure-function coverage (no provider, no DB) for the parts of Caption Display v2
(issue #188) that moved out of the deterministic chain: candidate derivation with
length filtering, the ID-only subagent validator, and the deterministic finalize
(punch cap + adjacency density + rect materialization).
"""

from __future__ import annotations

from packages.core.contracts.artifacts import EmphasisHint
from packages.production.pipeline._huazi_candidates import (
    HUAZI_MAX_CANDIDATES,
    HuaziPlanChoice,
    derive_huazi_candidates,
    finalize_huazi_plan,
    normal_caption_top_y,
    parse_huazi_plan,
    validate_huazi_plan,
)


def _units(*triples):
    return [{"text": t, "start": s, "end": e} for (t, s, e) in triples]


# --------------------------------------------------------------------------- #
# derive_huazi_candidates
# --------------------------------------------------------------------------- #
def test_derive_matches_phrase_to_narration_sentence():
    units = _units(("今天给大家带来限时五折活动", 0.0, 2.0), ("到店即可参与", 2.0, 3.0))
    events = derive_huazi_candidates([EmphasisHint(phrase="限时五折")], units)
    assert len(events) == 1
    assert events[0].text == "限时五折"
    assert events[0].event_id == "hz_001"
    assert (events[0].start, events[0].end) == (0.0, 2.0)


def test_derive_unmatched_phrase_dropped():
    events = derive_huazi_candidates(
        [EmphasisHint(phrase="限时五折")], _units(("完全不相关的一句话", 0.0, 2.0))
    )
    assert events == []


def test_derive_empty_emphasis_no_events():
    assert derive_huazi_candidates([], _units(("一句话", 0.0, 1.0))) == []


def test_derive_one_overlay_per_sentence():
    units = _units(("今天限时五折只要九块九", 0.0, 2.0), ("赶紧来", 2.0, 3.0))
    events = derive_huazi_candidates(
        [EmphasisHint(phrase="限时五折"), EmphasisHint(phrase="九块九")], units
    )
    assert len(events) == 1
    assert events[0].text == "限时五折"


def test_derive_filters_phrases_shorter_than_two_visual_chars():
    events = derive_huazi_candidates([EmphasisHint(phrase="五")], _units(("我要五折", 0.0, 2.0)))
    assert events == []


def test_derive_filters_phrases_longer_than_ten_visual_chars():
    long_phrase = "一二三四五六七八九十一"  # 11 visual chars
    events = derive_huazi_candidates(
        [EmphasisHint(phrase=long_phrase)], _units((f"开头{long_phrase}结尾", 0.0, 2.0))
    )
    assert events == []


def test_derive_keeps_two_and_ten_char_boundaries():
    units = _units(("五折五折", 0.0, 1.0), ("一二三四五六七八九十来了", 1.0, 2.0))
    events = derive_huazi_candidates(
        [EmphasisHint(phrase="五折"), EmphasisHint(phrase="一二三四五六七八九十")], units
    )
    assert [e.text for e in events] == ["五折", "一二三四五六七八九十"]


def test_derive_counts_only_visual_chars_for_length():
    # "8.5 折" has punctuation/space but 4 visual chars (8,5,折 -> plus 折? here 8,5,折)
    units = _units(("今天8.5 折", 0.0, 2.0))
    events = derive_huazi_candidates([EmphasisHint(phrase="8.5 折")], units)
    assert len(events) == 1
    assert events[0].text == "8.5 折"


def test_derive_caps_at_max_candidates():
    phrases = [EmphasisHint(phrase=f"短语{i}") for i in range(HUAZI_MAX_CANDIDATES + 3)]
    units = _units(*[(f"包含短语{i}的句子", float(i), float(i) + 1) for i in range(HUAZI_MAX_CANDIDATES + 3)])
    events = derive_huazi_candidates(phrases, units)
    assert len(events) == HUAZI_MAX_CANDIDATES
    assert [e.event_id for e in events] == [f"hz_{i + 1:03d}" for i in range(HUAZI_MAX_CANDIDATES)]


def test_derive_is_deterministic():
    units = _units(("今天限时五折", 0.0, 2.0))
    first = derive_huazi_candidates([EmphasisHint(phrase="限时五折")], units)
    second = derive_huazi_candidates([EmphasisHint(phrase="限时五折")], units)
    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]


# --------------------------------------------------------------------------- #
# normal_caption_top_y
# --------------------------------------------------------------------------- #
def test_normal_caption_top_y_reserves_two_lines():
    # 64px * 1.25 = 80px per line; 2 lines / 1920 = 0.0833; 0.88 - 0.0833 = 0.7967.
    assert normal_caption_top_y(position_y=0.88, font_size=64, canvas_height=1920) == 0.7967


def test_normal_caption_top_y_clamps_position_and_floor():
    assert normal_caption_top_y(position_y=1.5, font_size=64, canvas_height=1920) == 0.9167
    assert normal_caption_top_y(position_y=0.05, font_size=64, canvas_height=200) == 0.0


# --------------------------------------------------------------------------- #
# parse_huazi_plan
# --------------------------------------------------------------------------- #
def test_parse_valid_selection():
    choices, overreach, parse_errors = parse_huazi_plan(
        {
            "huazi": [
                {
                    "event_id": "hz_001",
                    "layout_box_id": "upper_center_medium",
                    "animation_id": "slide_up",
                    "priority": 3,
                    "reason": "醒目",
                }
            ]
        }
    )
    assert overreach == []
    assert parse_errors == []
    assert choices == [
        HuaziPlanChoice(
            event_id="hz_001",
            layout_box_id="upper_center_medium",
            animation_id="slide_up",
            priority=3,
            reason="醒目",
        )
    ]


def test_parse_defaults_animation_and_skips_garbage():
    choices, overreach, parse_errors = parse_huazi_plan(
        {"huazi": ["nope", {"layout_box_id": "x"}, {"event_id": "hz_002", "layout_box_id": "b"}]}
    )
    assert overreach == []
    assert parse_errors == [
        "huazi[0] must be an object",
        "huazi[1].event_id is required",
    ]
    assert len(choices) == 1
    assert choices[0].event_id == "hz_002"
    assert choices[0].animation_id == "pop_in"


def test_parse_flags_forbidden_fields_as_overreach():
    _choices, overreach, parse_errors = parse_huazi_plan(
        {"huazi": [{"event_id": "hz_001", "sfx_id": "ding", "y": 0.5, "text": "改写"}]}
    )
    assert overreach == ["huazi.sfx_id", "huazi.text", "huazi.y"]
    assert parse_errors == []


def test_parse_rejects_malformed_response_shape_but_allows_explicit_empty_selection():
    assert parse_huazi_plan(None) == (
        [],
        [],
        ["huazi response must be a JSON object"],
    )
    assert parse_huazi_plan({}) == (
        [],
        [],
        ["huazi response must include a 'huazi' array"],
    )
    assert parse_huazi_plan({"huazi": "nonsense"}) == (
        [],
        [],
        ["huazi response field 'huazi' must be an array"],
    )
    assert parse_huazi_plan({"huazi": []}) == ([], [], [])


# --------------------------------------------------------------------------- #
# validate_huazi_plan
# --------------------------------------------------------------------------- #
def _event(event_id, start=0.0, end=2.0, text="限时五折"):
    return {"event_id": event_id, "text": text, "start": start, "end": end}


def _box(box_id, directions, align="center"):
    return {
        "layout_box_id": box_id,
        "rect": {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.1},
        "text_align": align,
        "allowed_enter_directions": list(directions),
        "collision_score": 0.1,
        "region_tags": [],
    }


def _one_event_setup():
    candidate_events = [_event("hz_001")]
    boxes_by_event = {
        "hz_001": [_box("upper_left_medium", ["left", "up"], "left"), _box("top_center_medium", [])]
    }
    return candidate_events, boxes_by_event


def test_validate_accepts_known_choice():
    events, boxes = _one_event_setup()
    choice = HuaziPlanChoice("hz_001", "upper_left_medium", "slide_left", 2, "r")
    assert validate_huazi_plan([choice], candidate_events=events, boxes_by_event=boxes) == []


def test_validate_rejects_unknown_event():
    events, boxes = _one_event_setup()
    choice = HuaziPlanChoice("hz_999", "upper_left_medium", "pop_in", 1, "r")
    errors = validate_huazi_plan([choice], candidate_events=events, boxes_by_event=boxes)
    assert any("hz_999" in e and "not a known huazi candidate" in e for e in errors)


def test_validate_rejects_duplicate_event():
    events, boxes = _one_event_setup()
    choice = HuaziPlanChoice("hz_001", "upper_left_medium", "pop_in", 1, "r")
    errors = validate_huazi_plan(
        [choice, choice], candidate_events=events, boxes_by_event=boxes
    )
    assert any("selected more than once" in e for e in errors)


def test_validate_rejects_unknown_box():
    events, boxes = _one_event_setup()
    choice = HuaziPlanChoice("hz_001", "no_such_box", "pop_in", 1, "r")
    errors = validate_huazi_plan([choice], candidate_events=events, boxes_by_event=boxes)
    assert any("no_such_box" in e and "not a candidate box" in e for e in errors)


def test_validate_rejects_unknown_animation():
    events, boxes = _one_event_setup()
    choice = HuaziPlanChoice("hz_001", "upper_left_medium", "explode", 1, "r")
    errors = validate_huazi_plan([choice], candidate_events=events, boxes_by_event=boxes)
    assert any("explode" in e and "not a known animation" in e for e in errors)


def test_validate_rejects_slide_direction_not_allowed_by_box():
    events, boxes = _one_event_setup()
    # top_center_medium allows no slide directions; slide_up needs "up".
    choice = HuaziPlanChoice("hz_001", "top_center_medium", "slide_up", 1, "r")
    errors = validate_huazi_plan([choice], candidate_events=events, boxes_by_event=boxes)
    assert any("enters from 'up'" in e for e in errors)


def test_validate_allows_non_slide_animation_on_directionless_box():
    events, boxes = _one_event_setup()
    choice = HuaziPlanChoice("hz_001", "top_center_medium", "pop_in", 1, "r")
    assert validate_huazi_plan([choice], candidate_events=events, boxes_by_event=boxes) == []


def test_validate_rejects_overreach_fields():
    events, boxes = _one_event_setup()
    choice = HuaziPlanChoice("hz_001", "upper_left_medium", "pop_in", 1, "r")
    errors = validate_huazi_plan(
        [choice], candidate_events=events, boxes_by_event=boxes, overreach_fields=["huazi.sfx_id"]
    )
    assert any("forbidden fields" in e and "huazi.sfx_id" in e for e in errors)


def test_validate_rejects_more_than_max_events():
    events = [_event(f"hz_{i:03d}", start=float(i)) for i in range(HUAZI_MAX_CANDIDATES + 1)]
    boxes = {e["event_id"]: [_box("top_center_medium", [])] for e in events}
    choices = [HuaziPlanChoice(e["event_id"], "top_center_medium", "pop_in", 1, "r") for e in events]
    errors = validate_huazi_plan(choices, candidate_events=events, boxes_by_event=boxes)
    assert any("exceeds the maximum" in e for e in errors)


# --------------------------------------------------------------------------- #
# finalize_huazi_plan
# --------------------------------------------------------------------------- #
def test_finalize_materializes_box_rect_into_overlay_event():
    events = [_event("hz_001", start=0.0, end=2.0, text="限时五折")]
    boxes = {"hz_001": [_box("upper_left_medium", ["left", "up"], "left")]}
    choice = HuaziPlanChoice("hz_001", "upper_left_medium", "slide_left", 4, "醒目")

    result = finalize_huazi_plan([choice], candidate_events=events, boxes_by_event=boxes)

    assert result.animation_fallbacks == 0
    assert result.density_drops == 0
    [event] = result.overlay_events
    assert event.event_id == "hz_001"
    assert event.text == "限时五折"
    assert event.animation_id == "slide_left"
    assert event.layout_box_id == "upper_left_medium"
    assert event.text_align == "left"
    assert event.priority == 4
    assert event.sfx_id == "none"
    assert (event.rect.x, event.rect.y, event.rect.w, event.rect.h) == (0.1, 0.1, 0.4, 0.1)
    assert result.choices[0]["layout_box_id"] == "upper_left_medium"


def test_finalize_caps_punch_to_two_downgrading_lowest_priority():
    # Three punches spaced far apart (no adjacency interaction); lowest priority
    # deterministically downgrades to pop_in.
    events = [
        _event("hz_001", start=0.0, end=1.0),
        _event("hz_002", start=3.0, end=4.0),
        _event("hz_003", start=6.0, end=7.0),
    ]
    boxes = {e["event_id"]: [_box("top_center_medium", [])] for e in events}
    choices = [
        HuaziPlanChoice("hz_001", "top_center_medium", "punch", 5, "a"),
        HuaziPlanChoice("hz_002", "top_center_medium", "punch", 3, "b"),
        HuaziPlanChoice("hz_003", "top_center_medium", "punch", 1, "c"),
    ]

    result = finalize_huazi_plan(choices, candidate_events=events, boxes_by_event=boxes)

    assert result.animation_fallbacks == 1
    animations = {e.event_id: e.animation_id for e in result.overlay_events}
    assert animations == {"hz_001": "punch", "hz_002": "punch", "hz_003": "pop_in"}


def test_finalize_drops_adjacent_event_keeping_higher_priority():
    events = [
        _event("hz_001", start=0.0, end=1.0),
        _event("hz_002", start=1.2, end=2.0),
    ]
    boxes = {e["event_id"]: [_box("top_center_medium", [])] for e in events}
    choices = [
        HuaziPlanChoice("hz_001", "top_center_medium", "pop_in", 1, "low"),
        HuaziPlanChoice("hz_002", "top_center_medium", "pop_in", 5, "high"),
    ]

    result = finalize_huazi_plan(choices, candidate_events=events, boxes_by_event=boxes)

    assert result.density_drops == 1
    assert [e.event_id for e in result.overlay_events] == ["hz_002"]


def test_finalize_adjacency_tie_break_keeps_earlier_start():
    events = [
        _event("hz_001", start=0.0, end=1.0),
        _event("hz_002", start=1.5, end=2.0),
    ]
    boxes = {e["event_id"]: [_box("top_center_medium", [])] for e in events}
    choices = [
        HuaziPlanChoice("hz_001", "top_center_medium", "pop_in", 2, "a"),
        HuaziPlanChoice("hz_002", "top_center_medium", "pop_in", 2, "b"),
    ]

    result = finalize_huazi_plan(choices, candidate_events=events, boxes_by_event=boxes)

    assert result.density_drops == 1
    assert [e.event_id for e in result.overlay_events] == ["hz_001"]


def test_finalize_keeps_well_spaced_events_in_start_order():
    events = [
        _event("hz_002", start=5.0, end=6.0),
        _event("hz_001", start=0.0, end=1.0),
    ]
    boxes = {e["event_id"]: [_box("top_center_medium", [])] for e in events}
    choices = [
        HuaziPlanChoice("hz_002", "top_center_medium", "pop_in", 1, "b"),
        HuaziPlanChoice("hz_001", "top_center_medium", "pop_in", 1, "a"),
    ]

    result = finalize_huazi_plan(choices, candidate_events=events, boxes_by_event=boxes)

    assert result.density_drops == 0
    assert [e.event_id for e in result.overlay_events] == ["hz_001", "hz_002"]
