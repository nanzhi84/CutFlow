"""Pure-function tests for the EditingAgentPlanning helpers (issue #136).

Covers the acceptance criteria that don't need a provider: a valid selection
materializes into frame-exact portrait/broll/style artifacts, an invalid ID is
rejected by the validator, the empty-font-pool path falls back to the default
font, a valid BGM id produces a BgmPlan, and b-roll overlays carry complete,
non-overlapping frame fields.
"""

from __future__ import annotations

from packages.core.contracts import DigitalHumanVideoRequest
from packages.production.pipeline._editing_agent import (
    BrollChoice,
    EditingSelection,
    PortraitChoice,
    build_agent_input,
    deterministic_selection,
    index_candidates,
    materialize_broll,
    materialize_portrait,
    materialize_style,
    parse_selection,
    portrait_cut_frames,
    select_with_repair,
    validate_selection,
)


def _request(**edit) -> DigitalHumanVideoRequest:
    return DigitalHumanVideoRequest(
        case_id="case_demo",
        script="今天带你看一下这套案例。第一步先看施工前的样子。",
        title="案例",
        voice={"voice_id": "voice_sandbox"},
        edit=edit or {},
    )


def _boundary() -> dict:
    return {
        "fps": 30,
        "total_frames": 360,
        "safe_cut_boundaries": [
            {"cut_id": "cut_000", "time": 0.0, "frame": 0, "source": "semantic_only"},
            {"cut_id": "cut_001", "time": 6.0, "frame": 180, "source": "semantic_audio_pause"},
            {"cut_id": "cut_002", "time": 12.0, "frame": 360, "source": "semantic_only"},
        ],
        "portrait_slots": [
            {
                "slot_id": "pslot_000",
                "start_frame": 0,
                "end_frame": 180,
                "unit_ids": ["unit_001"],
                "boundary_source": "semantic_audio_pause",
            },
            {
                "slot_id": "pslot_001",
                "start_frame": 180,
                "end_frame": 360,
                "unit_ids": ["unit_002"],
                "boundary_source": "semantic_only",
            },
        ],
        "broll_slots": [
            {
                "slot_id": "bslot_000",
                "start_frame": 60,
                "end_frame": 120,
                "unit_ids": ["unit_001"],
                "text": "施工前",
            },
            {
                "slot_id": "bslot_001",
                "start_frame": 210,
                "end_frame": 270,
                "unit_ids": ["unit_002"],
                "text": "施工过程",
            },
        ],
    }


def _material(*, with_font=True, with_bgm=True, short_portrait=False) -> dict:
    portrait = [
        {
            "asset_id": "portrait_a",
            "score": 90.0,
            "reason": "白色上衣，稳定口播",
            "metadata": {"clip_id": "clip_a", "source_start": 0.0, "source_end": 20.0},
        },
        {
            "asset_id": "portrait_b",
            "score": 70.0,
            "reason": "黑色上衣",
            "metadata": {
                "clip_id": "clip_b",
                "source_start": 0.0,
                "source_end": 2.0 if short_portrait else 18.0,
            },
        },
    ]
    broll = [
        {
            "asset_id": "broll_x",
            "score": 80.0,
            "reason": "施工前画面",
            "metadata": {
                "clip_id": "clip_x",
                "source_start": 0.0,
                "source_end": 6.0,
                "scene_name": "工地/施工前",
                "matched_keywords": ["施工前"],
            },
        },
        {
            "asset_id": "broll_y",
            "score": 60.0,
            "reason": "施工过程",
            "metadata": {
                "clip_id": "clip_y",
                "source_start": 0.0,
                "source_end": 5.0,
                "scene_name": "工地/施工中",
            },
        },
    ]
    font = [{"asset_id": "font_yst", "score": 50.0, "reason": "清晰标题字体"}] if with_font else []
    bgm = (
        [
            {
                "asset_id": "bgm_001",
                "score": 75.0,
                "reason": "稳定不抢人声",
                "metadata": {
                    "clip_id": "bgm_clip_1",
                    "source_start": 0.0,
                    "source_end": 60.0,
                    "duration": 60.0,
                    "section_type": "stable_bed",
                    "mood": "励志",
                    "energy_profile": "medium",
                    "loopable": True,
                    "script_fit": ["案例"],
                    "scene_fit": ["工地"],
                },
            }
        ]
        if with_bgm
        else []
    )
    return {
        "case_id": "case_demo",
        "portrait_candidates": portrait,
        "broll_candidates": broll,
        "font_candidates": font,
        "bgm_candidates": bgm,
    }


def _valid_selection() -> EditingSelection:
    return EditingSelection(
        portrait=[
            PortraitChoice(slot_id="pslot_000", window_id="pc_000", reason="穿搭一致"),
            PortraitChoice(slot_id="pslot_001", window_id="pc_001", reason="保持连续"),
        ],
        broll=[
            BrollChoice(
                slot_id="bslot_000",
                candidate_id="bc_000",
                reason="施工前贴合",
                confidence=0.86,
                matched_keywords=("施工前",),
            ),
            BrollChoice(
                slot_id="bslot_001", candidate_id="bc_001", reason="施工过程", confidence=0.7
            ),
        ],
        font_id="font_yst",
        bgm_id="bgm_001",
    )


# --------------------------------------------------------------------------- #
def test_index_and_build_agent_input_number_candidates():
    material = _material()
    candidates = index_candidates(material)
    assert set(candidates.portrait_by_id) == {"pc_000", "pc_001"}
    assert set(candidates.broll_by_id) == {"bc_000", "bc_001"}
    assert set(candidates.font_by_id) == {"font_yst"}
    assert set(candidates.bgm_by_id) == {"bgm_001"}

    payload = build_agent_input(
        request=_request(instruction="尽量用穿搭相近的人像"),
        boundary=_boundary(),
        candidates=candidates,
        narration_units=[
            {"unit_id": "unit_001", "text": "今天带你看一下这套案例", "start": 0.0, "end": 6.0}
        ],
        duration=12.0,
    )
    assert payload["edit_instruction"] == "尽量用穿搭相近的人像"
    assert payload["video_duration"] == 12.0
    assert [c["candidate_id"] for c in payload["portrait_candidates"]] == ["pc_000", "pc_001"]
    assert payload["portrait_candidates"][0]["source_end"] == 20.0
    assert payload["max_broll_inserts"] == 4


def test_valid_selection_passes_validation():
    errors = validate_selection(
        _valid_selection(),
        boundary=_boundary(),
        candidates=index_candidates(_material()),
        bgm_enabled=True,
    )
    assert errors == []


def test_invalid_ids_and_missing_coverage_are_rejected():
    candidates = index_candidates(_material())
    # unknown window + a portrait slot left uncovered
    bad = EditingSelection(
        portrait=[PortraitChoice(slot_id="pslot_000", window_id="pc_999")],
        broll=[BrollChoice(slot_id="bslot_000", candidate_id="bc_404")],
        font_id="font_missing",
        bgm_id="bgm_missing",
    )
    errors = validate_selection(bad, boundary=_boundary(), candidates=candidates, bgm_enabled=True)
    joined = " | ".join(errors)
    assert "pc_999" in joined  # unknown portrait candidate
    assert "pslot_001" in joined  # uncovered slot
    assert "bc_404" in joined  # unknown broll candidate
    assert "font_missing" in joined
    assert "bgm_missing" in joined


def test_short_source_window_is_rejected():
    # portrait_b source is only 2s (60 frames) but pslot_001 needs 180 frames.
    candidates = index_candidates(_material(short_portrait=True))
    selection = EditingSelection(
        portrait=[
            PortraitChoice(slot_id="pslot_000", window_id="pc_000"),
            PortraitChoice(slot_id="pslot_001", window_id="pc_001"),
        ]
    )
    errors = validate_selection(
        selection, boundary=_boundary(), candidates=candidates, bgm_enabled=False
    )
    assert any("too short" in e for e in errors)


def test_deterministic_selection_is_valid_and_covers_all_slots():
    boundary = _boundary()
    candidates = index_candidates(_material())
    selection = deterministic_selection(
        boundary=boundary, candidates=candidates, bgm_enabled=True, max_inserts=4
    )
    assert {c.slot_id for c in selection.portrait} == {"pslot_000", "pslot_001"}
    # top-scored portrait (portrait_a=pc_000) chosen for every slot (uniqueness relaxed)
    assert all(c.window_id == "pc_000" for c in selection.portrait)
    assert selection.font_id == "font_yst"
    assert selection.bgm_id == "bgm_001"
    assert (
        validate_selection(selection, boundary=boundary, candidates=candidates, bgm_enabled=True)
        == []
    )


def test_materialize_portrait_frames_are_complete_and_contiguous():
    payload = materialize_portrait(
        selection=_valid_selection(), boundary=_boundary(), candidates=index_candidates(_material())
    )
    segments = payload["segments"]
    assert len(segments) == 2
    for seg in segments:
        for key in (
            "timeline_start_frame",
            "timeline_end_frame",
            "source_start_frame",
            "source_end_frame",
        ):
            assert isinstance(seg[key], int)
        assert seg["source_end_frame"] > seg["source_start_frame"]
        assert seg["slot_phase"] in {"portrait_opening", "portrait_main"}
    # contiguous timeline covering the whole grid [0, 360)
    assert segments[0]["timeline_start_frame"] == 0
    assert segments[0]["timeline_end_frame"] == segments[1]["timeline_start_frame"] == 180
    assert segments[1]["timeline_end_frame"] == 360


def test_materialize_broll_overlays_have_frames_and_no_overlap():
    boundary = _boundary()
    candidates = index_candidates(_material())
    portrait_payload = materialize_portrait(
        selection=_valid_selection(), boundary=boundary, candidates=candidates
    )
    payload = materialize_broll(
        selection=_valid_selection(),
        boundary=boundary,
        candidates=candidates,
        cut_frames=portrait_cut_frames(portrait_payload),
        enabled=True,
        max_inserts=4,
    )
    overlays = payload["overlays"]
    assert payload["enabled"] is True
    assert len(overlays) == 2
    for ov in overlays:
        for key in (
            "timeline_start_frame",
            "timeline_end_frame",
            "source_start_frame",
            "source_end_frame",
        ):
            assert isinstance(ov[key], int)
        assert ov["timeline_end_frame"] > ov["timeline_start_frame"]
        assert ov["source_end_frame"] > ov["source_start_frame"]
    ordered = sorted(overlays, key=lambda o: o["timeline_start_frame"])
    assert ordered[0]["timeline_end_frame"] <= ordered[1]["timeline_start_frame"]


def test_materialize_broll_disabled_returns_empty():
    payload = materialize_broll(
        selection=_valid_selection(),
        boundary=_boundary(),
        candidates=index_candidates(_material()),
        cut_frames=[0, 180, 360],
        enabled=False,
        max_inserts=4,
    )
    assert payload["enabled"] is False
    assert payload["overlays"] == []


def test_materialize_style_uses_chosen_font_and_bgm():
    payload = materialize_style(
        selection=_valid_selection(),
        candidates=index_candidates(_material()),
        request=_request(),
        overlay_events=[],
    )
    assert payload["font_asset_id"] == "font_yst"
    assert payload["font"]["font_id"] == "font_yst"
    assert payload["bgm"] is not None
    assert payload["bgm"]["asset_id"] == "bgm_001"
    assert payload["bgm"]["mood"] == "励志"
    assert payload["bgm"]["section_type"] == "stable_bed"


def test_materialize_style_empty_font_pool_falls_back_to_default():
    payload = materialize_style(
        selection=EditingSelection(font_id=None, bgm_id=None),
        candidates=index_candidates(_material(with_font=False, with_bgm=False)),
        request=_request(),
        overlay_events=[],
    )
    assert payload["font_asset_id"] == "case_default_font"
    assert payload["bgm"] is None


def test_materialize_style_bgm_disabled_yields_no_bgm():
    req = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_sandbox"},
        bgm={"enabled": False},
    )
    payload = materialize_style(
        selection=_valid_selection(),
        candidates=index_candidates(_material()),
        request=req,
        overlay_events=[],
    )
    assert payload["bgm"] is None


def test_parse_selection_is_robust_to_garbage():
    parsed = parse_selection(
        {"portrait_plan": "nonsense", "broll_plan": [{"slot_id": "b", "candidate_id": "c"}]}
    )
    assert parsed.portrait == []
    assert parsed.broll[0].slot_id == "b"
    assert parse_selection(None).portrait == []


def test_select_with_repair_recovers_from_invalid_then_valid():
    boundary, candidates = _boundary(), index_candidates(_material())
    outputs = iter(
        [
            {"portrait_plan": [{"slot_id": "pslot_000", "window_id": "pc_999"}]},  # invalid
            {  # valid on repair
                "portrait_plan": [
                    {"slot_id": "pslot_000", "window_id": "pc_000"},
                    {"slot_id": "pslot_001", "window_id": "pc_001"},
                ],
                "broll_plan": [],
                "font_plan": {"font_id": "font_yst"},
                "bgm_plan": {"bgm_id": "bgm_001"},
            },
        ]
    )
    seen_prev_errors: list[list[str]] = []

    def invoke(prev_errors):
        seen_prev_errors.append(list(prev_errors))
        return next(outputs)

    selection, trace, errors = select_with_repair(
        invoke=invoke,
        boundary=boundary,
        candidates=candidates,
        bgm_enabled=True,
        max_repair_attempts=1,
    )
    assert errors == []  # repaired to a valid selection
    assert len(trace) == 2  # first attempt + one repair
    assert seen_prev_errors[0] == []  # first call has no prior errors
    assert seen_prev_errors[1]  # repair call received the validator's errors


def test_select_with_repair_gives_up_after_budget():
    boundary, candidates = _boundary(), index_candidates(_material())

    def invoke(_prev_errors):
        return {
            "portrait_plan": [{"slot_id": "pslot_000", "window_id": "pc_999"}]
        }  # always invalid

    _selection, trace, errors = select_with_repair(
        invoke=invoke,
        boundary=boundary,
        candidates=candidates,
        bgm_enabled=False,
        max_repair_attempts=1,
    )
    assert errors  # still invalid after the repair budget
    assert len(trace) == 2  # 1 initial + 1 repair attempt


def test_materialize_broll_drops_sub_frame_overlay():
    # A broll candidate whose usable source window is shorter than one 30fps frame
    # would quantize to a zero-length overlay; it must be dropped, not emitted.
    material = _material()
    material["broll_candidates"] = [
        {
            "asset_id": "broll_tiny",
            "score": 80.0,
            "metadata": {"clip_id": "c", "source_start": 1.0, "source_end": 1.01},
        }
    ]
    candidates = index_candidates(material)
    selection = EditingSelection(broll=[BrollChoice(slot_id="bslot_000", candidate_id="bc_000")])
    payload = materialize_broll(
        selection=selection,
        boundary=_boundary(),
        candidates=candidates,
        cut_frames=[0, 180, 360],
        enabled=True,
        max_inserts=4,
    )
    assert payload["overlays"] == []
