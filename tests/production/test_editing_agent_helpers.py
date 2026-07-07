"""Pure-function tests for the EditingAgentPlanning helpers (issue #136).

Covers the acceptance criteria that don't need a provider: a valid selection
materializes into frame-exact portrait/broll/style artifacts, an invalid ID is
rejected by the validator, the empty-font-pool path falls back to the default
font, a valid BGM id produces a BgmPlan, and b-roll overlays carry complete,
non-overlapping frame fields.
"""

from __future__ import annotations

from pathlib import Path

from packages.core.contracts import DigitalHumanVideoRequest, WarningCode
from packages.planning.material import shortlist_for_windows
from packages.planning.material.broll_plan import BROLL_GEOMETRY_POLICY
from packages.production.pipeline import _editing_agent
from packages.production.pipeline._editing_agent import (
    BrollChoice,
    EditingSelection,
    PortraitChoice,
    build_agent_input,
    deterministic_selection,
    index_candidates,
    parse_selection,
    select_with_repair,
    validate_selection,
)
from packages.production.pipeline._materialize import (
    materialize_broll_from_assignment,
    materialize_portrait_from_assignment,
    materialize_style_from_selection,
    portrait_cut_frames,
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
                "start_frame": 240,
                "end_frame": 300,
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
            "metadata": {
                "clip_id": "clip_a",
                "source_start": 0.0,
                "source_end": 20.0,
                "description": "白色上衣稳定口播",
            },
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
                "description": "施工前墙面状态特写",
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
    font = (
        [{"asset_id": "font_yst", "score": 50.0, "reason": "清晰标题字体"}]
        if with_font
        else []
    )
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


def _windows(boundary: dict) -> dict:
    return {
        "fps": int(boundary.get("fps") or 30),
        "total_frames": int(boundary.get("total_frames") or 0),
        "portrait_windows": [
            {
                "window_id": slot["slot_id"],
                "start_frame": slot["start_frame"],
                "end_frame": slot["end_frame"],
                "unit_ids": list(slot.get("unit_ids") or []),
                "boundary_source": slot.get("boundary_source"),
                "phase": "opening" if index == 0 else "main",
            }
            for index, slot in enumerate(boundary.get("portrait_slots") or [])
        ],
        "broll_windows": [
            {
                "window_id": slot["slot_id"],
                "start_frame": slot["start_frame"],
                "end_frame": slot["end_frame"],
                "length_frames": slot["end_frame"] - slot["start_frame"],
                "source_length_frames": slot.get(
                    "source_length_frames",
                    slot["end_frame"] - slot["start_frame"],
                ),
                "pad_start": slot.get("pad_start", 0.0),
                "pad_end": slot.get("pad_end", 0.0),
                "host_unit_ids": list(slot.get("unit_ids") or []),
                "host_portrait_window_ids": [],
                "text": slot.get("text") or "",
                "boundary_source": slot.get("boundary_source") or "narration_unit",
            }
            for slot in boundary.get("broll_slots") or []
        ],
    }


def _assignment(selection: EditingSelection) -> dict:
    return {
        "portrait": [
            {
                "window_id": choice.slot_id,
                "candidate_id": choice.window_id,
                "source_mode": choice.source_mode,
                "reason": choice.reason,
            }
            for choice in selection.portrait
        ],
        "broll": [
            {
                "window_id": choice.slot_id,
                "candidate_id": choice.candidate_id,
                "reason": choice.reason,
                "confidence": choice.confidence,
                "matched_keywords": list(choice.matched_keywords),
            }
            for choice in selection.broll
        ],
    }


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
            {
                "unit_id": "unit_001",
                "text": "今天带你看一下这套案例",
                "start": 0.0,
                "end": 6.0,
            }
        ],
        duration=12.0,
    )
    assert payload["edit_instruction"] == "尽量用穿搭相近的人像"
    assert payload["video_duration"] == 12.0
    assert [c["candidate_id"] for c in payload["portrait_candidates"]] == ["pc_000", "pc_001"]
    assert payload["portrait_candidates"][0]["source_end"] == 20.0
    assert payload["portrait_candidates"][0]["available_frames"] == 600
    assert payload["portrait_candidates"][0]["description"] == "白色上衣稳定口播"
    assert payload["broll_candidates"][0]["description"] == "施工前墙面状态特写"
    assert payload["portrait_slots"][0]["required_frames"] == 180
    assert payload["portrait_slots"][0]["required_seconds"] == 6.0
    assert payload["portrait_slots"][0]["legal_window_ids"] == ["pc_000", "pc_001"]
    assert payload["max_broll_inserts"] == 4


def test_agent_input_marks_short_portraits_illegal_per_slot():
    payload = build_agent_input(
        request=_request(),
        boundary=_boundary(),
        candidates=index_candidates(_material(short_portrait=True)),
        narration_units=[],
        duration=12.0,
    )

    assert payload["portrait_candidates"][1]["available_frames"] == 60
    assert payload["portrait_slots"][0]["required_frames"] == 180
    assert payload["portrait_slots"][0]["legal_window_ids"] == ["pc_000"]
    assert payload["portrait_slots"][1]["legal_window_ids"] == ["pc_000"]


def test_agent_portrait_avoid_spans_trim_legal_windows_and_shortlist():
    material = _material()
    material["portrait_candidates"] = [
        {
            "asset_id": "portrait_bad",
            "score": 90.0,
            "metadata": {
                "clip_id": "bad",
                "source_start": 0.0,
                "source_end": 8.0,
                "avoid_spans": [[2.0, 8.0]],
            },
        },
        {
            "asset_id": "portrait_clean",
            "score": 80.0,
            "metadata": {
                "clip_id": "clean",
                "source_start": 0.0,
                "source_end": 8.0,
                "avoid_spans": [[0.0, 1.0]],
            },
        },
    ]
    material["broll_candidates"] = []

    payload = build_agent_input(
        request=_request(),
        boundary=_boundary(),
        candidates=index_candidates(material),
        narration_units=[],
        duration=12.0,
    )

    assert payload["portrait_slots"][0]["legal_window_ids"] == ["pc_001"]
    assert payload["portrait_slots"][1]["legal_window_ids"] == ["pc_001"]
    assert payload["portrait_candidates"][0]["source_start"] == 0.0
    assert payload["portrait_candidates"][0]["source_end"] == 2.0
    assert payload["portrait_candidates"][0]["available_frames"] == 60
    assert payload["portrait_candidates"][1]["source_start"] == 1.0
    assert payload["portrait_candidates"][1]["source_end"] == 8.0
    assert payload["portrait_candidates"][1]["available_frames"] == 210

    shortlisted, counts = shortlist_for_windows(
        _windows(_boundary())["portrait_windows"],
        [],
        material,
    )
    assert [c["asset_id"] for c in shortlisted["portrait_candidates"]] == ["portrait_clean"]
    assert counts["portrait"] == {"raw": 2, "eligible": 1, "exposed": 1, "dropped": 1}


def test_valid_selection_passes_validation():
    errors = validate_selection(
        _valid_selection(),
        boundary=_boundary(),
        candidates=index_candidates(_material()),
        bgm_enabled=True,
    )
    assert errors == []


def test_full_coverage_validation_reports_missing_broll_slots_for_repair():
    selection = EditingSelection(
        portrait=_valid_selection().portrait,
        broll=[BrollChoice(slot_id="bslot_000", candidate_id="bc_000")],
        font_id="font_yst",
        bgm_id="bgm_001",
    )
    boundary = _boundary()
    candidates = index_candidates(_material())

    assert (
        validate_selection(
            selection,
            boundary=boundary,
            candidates=candidates,
            bgm_enabled=True,
        )
        == []
    )

    errors = validate_selection(
        selection,
        boundary=boundary,
        candidates=candidates,
        bgm_enabled=True,
        retrieval_topk_by_window={"bslot_001": ["bc_001"]},
        require_broll_coverage=True,
    )

    joined = " | ".join(errors)
    assert "broll slots not covered: bslot_001" in joined
    assert "full_coverage requires every broll slot" in joined
    assert "bslot_001 topK: bc_001" in joined


def test_select_with_repair_sends_missing_broll_slots_back_to_agent():
    attempts: list[list[str]] = []
    outputs = [
        {
            "portrait_plan": [
                {"slot_id": "pslot_000", "window_id": "pc_000"},
                {"slot_id": "pslot_001", "window_id": "pc_001"},
            ],
            "broll_plan": [{"slot_id": "bslot_000", "candidate_id": "bc_000"}],
            "font_plan": {"font_id": "font_yst"},
            "bgm_plan": {"bgm_id": "bgm_001"},
        },
        {
            "portrait_plan": [
                {"slot_id": "pslot_000", "window_id": "pc_000"},
                {"slot_id": "pslot_001", "window_id": "pc_001"},
            ],
            "broll_plan": [
                {"slot_id": "bslot_000", "candidate_id": "bc_000"},
                {"slot_id": "bslot_001", "candidate_id": "bc_001"},
            ],
            "font_plan": {"font_id": "font_yst"},
            "bgm_plan": {"bgm_id": "bgm_001"},
        },
    ]

    def invoke(previous_errors: list[str]):
        attempts.append(previous_errors)
        return outputs.pop(0)

    selection, trace, errors = select_with_repair(
        invoke=invoke,
        boundary=_boundary(),
        candidates=index_candidates(_material()),
        bgm_enabled=True,
        max_repair_attempts=1,
        retrieval_topk_by_window={"bslot_000": ["bc_000"], "bslot_001": ["bc_001"]},
        require_broll_coverage=True,
    )

    assert errors == []
    assert [choice.slot_id for choice in selection.broll] == ["bslot_000", "bslot_001"]
    assert trace[0]["error_count"] == 1
    assert "broll slots not covered: bslot_001" in attempts[1][0]


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
    assert any("requires 180 frames" in e and "pc_000" in e for e in errors)


def test_short_broll_source_window_is_rejected():
    material = _material()
    material["broll_candidates"][0]["metadata"]["source_end"] = 1.0
    candidates = index_candidates(material)
    selection = EditingSelection(
        portrait=[
            PortraitChoice(slot_id="pslot_000", window_id="pc_000"),
            PortraitChoice(slot_id="pslot_001", window_id="pc_001"),
        ],
        broll=[BrollChoice(slot_id="bslot_000", candidate_id="bc_000")],
    )

    errors = validate_selection(
        selection, boundary=_boundary(), candidates=candidates, bgm_enabled=False
    )

    assert any("broll candidate 'bc_000' source is too short" in e for e in errors)
    assert any("requires 60 frames" in e for e in errors)


def test_reusing_same_portrait_asset_is_rejected():
    candidates = index_candidates(_material())
    selection = EditingSelection(
        portrait=[
            PortraitChoice(slot_id="pslot_000", window_id="pc_000"),
            PortraitChoice(slot_id="pslot_001", window_id="pc_000"),
        ]
    )

    errors = validate_selection(
        selection, boundary=_boundary(), candidates=candidates, bgm_enabled=False
    )

    assert any("asset_id 'portrait_a' is assigned to more than one slot" in e for e in errors)


def test_reusing_same_broll_candidate_and_asset_is_rejected():
    material = _material()
    material["broll_candidates"].append(
        {
            "asset_id": "broll_x",
            "score": 40.0,
            "reason": "同素材另一个片段",
            "metadata": {
                "clip_id": "clip_x_alt",
                "source_start": 6.0,
                "source_end": 12.0,
                "scene_name": "工地/施工前",
            },
        }
    )
    candidates = index_candidates(material)
    selection = EditingSelection(
        portrait=[
            PortraitChoice(slot_id="pslot_000", window_id="pc_000"),
            PortraitChoice(slot_id="pslot_001", window_id="pc_001"),
        ],
        broll=[
            BrollChoice(slot_id="bslot_000", candidate_id="bc_000"),
            BrollChoice(slot_id="bslot_001", candidate_id="bc_000"),
        ],
    )

    errors = validate_selection(
        selection, boundary=_boundary(), candidates=candidates, bgm_enabled=False
    )

    assert any("broll candidate_id 'bc_000' is assigned more than once" in e for e in errors)

    selection = EditingSelection(
        portrait=[
            PortraitChoice(slot_id="pslot_000", window_id="pc_000"),
            PortraitChoice(slot_id="pslot_001", window_id="pc_001"),
        ],
        broll=[
            BrollChoice(slot_id="bslot_000", candidate_id="bc_000"),
            BrollChoice(slot_id="bslot_001", candidate_id="bc_002"),
        ],
    )

    errors = validate_selection(
        selection, boundary=_boundary(), candidates=candidates, bgm_enabled=False
    )

    assert any("broll asset_id 'broll_x' is assigned to more than one slot" in e for e in errors)


def test_deterministic_selection_is_valid_and_covers_all_slots():
    boundary = _boundary()
    candidates = index_candidates(_material())
    selection = deterministic_selection(
        boundary=boundary, candidates=candidates, bgm_enabled=True, max_inserts=4
    )
    assert {c.slot_id for c in selection.portrait} == {"pslot_000", "pslot_001"}
    assert [c.window_id for c in selection.portrait] == ["pc_000", "pc_001"]
    assert selection.font_id == "font_yst"
    assert selection.bgm_id == "bgm_001"
    assert (
        validate_selection(selection, boundary=boundary, candidates=candidates, bgm_enabled=True)
        == []
    )


def _three_slot_boundary() -> dict:
    boundary = _boundary()
    boundary["portrait_slots"] = [
        {"slot_id": "pslot_000", "start_frame": 0, "end_frame": 120, "unit_ids": ["unit_001"]},
        {"slot_id": "pslot_001", "start_frame": 120, "end_frame": 240, "unit_ids": ["unit_002"]},
        {"slot_id": "pslot_002", "start_frame": 240, "end_frame": 360, "unit_ids": ["unit_003"]},
    ]
    return boundary


def _three_asset_material() -> dict:
    material = _material()
    material["portrait_candidates"].append(
        {
            "asset_id": "portrait_c",
            "score": 60.0,
            "reason": "蓝色上衣",
            "metadata": {"clip_id": "clip_c", "source_start": 0.0, "source_end": 18.0},
        }
    )
    return material


def test_strict_uniqueness_rejects_reused_portrait_asset():
    boundary = _three_slot_boundary()
    candidates = index_candidates(_material())
    over = EditingSelection(
        portrait=[
            PortraitChoice(slot_id="pslot_000", window_id="pc_000"),
            PortraitChoice(slot_id="pslot_001", window_id="pc_000"),
            PortraitChoice(slot_id="pslot_002", window_id="pc_001"),
        ]
    )
    errors = validate_selection(over, boundary=boundary, candidates=candidates, bgm_enabled=False)
    assert any("assigned to more than one slot" in e for e in errors)


def test_deterministic_selection_keeps_portrait_assets_unique():
    boundary = _three_slot_boundary()
    candidates = index_candidates(_three_asset_material())
    selection = deterministic_selection(
        boundary=boundary, candidates=candidates, bgm_enabled=False, max_inserts=0
    )
    assert [c.slot_id for c in selection.portrait] == ["pslot_000", "pslot_001", "pslot_002"]
    assert [c.window_id for c in selection.portrait] == ["pc_000", "pc_001", "pc_002"]
    assert (
        validate_selection(selection, boundary=boundary, candidates=candidates, bgm_enabled=False)
        == []
    )


def test_deterministic_selection_leaves_unfillable_slot_uncovered_instead_of_reusing():
    boundary = _three_slot_boundary()
    candidates = index_candidates(_material())
    selection = deterministic_selection(
        boundary=boundary, candidates=candidates, bgm_enabled=False, max_inserts=0
    )
    assert [c.slot_id for c in selection.portrait] == ["pslot_000", "pslot_001"]
    assert [c.window_id for c in selection.portrait] == ["pc_000", "pc_001"]
    assert (
        validate_selection(selection, boundary=boundary, candidates=candidates, bgm_enabled=False)
        != []
    )


def test_deterministic_selection_counts_max_inserts_by_accepted_broll():
    boundary = {
        "broll_slots": [
            {"slot_id": "bslot_too_long", "start_frame": 0, "end_frame": 300},
            {"slot_id": "bslot_fillable", "start_frame": 300, "end_frame": 360},
        ]
    }
    material = _material()
    material["broll_candidates"] = [
        {
            "asset_id": "broll_short",
            "score": 90.0,
            "metadata": {"clip_id": "short_clip", "source_start": 0.0, "source_end": 2.0},
        }
    ]

    selection = deterministic_selection(
        boundary=boundary,
        candidates=index_candidates(material),
        bgm_enabled=False,
        max_inserts=1,
    )

    assert [(choice.slot_id, choice.candidate_id) for choice in selection.broll] == [
        ("bslot_fillable", "bc_000")
    ]


def test_materialize_portrait_frames_are_complete_and_contiguous():
    selection = _valid_selection()
    payload = materialize_portrait_from_assignment(
        windows=_windows(_boundary()),
        assignment=_assignment(selection),
        candidates=index_candidates(_material()),
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


def test_materialize_portrait_uses_clean_span_for_source_slice():
    material = _material()
    material["portrait_candidates"] = [
        {
            "asset_id": "portrait_clean",
            "score": 90.0,
            "metadata": {
                "clip_id": "clean",
                "source_start": 0.0,
                "source_end": 8.0,
                "avoid_spans": [[0.0, 1.0], [7.0, 8.0]],
            },
        }
    ]
    boundary = _boundary()
    boundary["portrait_slots"] = [
        {"slot_id": "pslot_000", "start_frame": 0, "end_frame": 180, "unit_ids": ["unit_001"]}
    ]
    selection = EditingSelection(
        portrait=[PortraitChoice(slot_id="pslot_000", window_id="pc_000")]
    )

    payload = materialize_portrait_from_assignment(
        windows=_windows(boundary),
        assignment=_assignment(selection),
        candidates=index_candidates(material),
    )

    [segment] = payload["segments"]
    assert segment["source_start_frame"] == 30
    assert segment["source_end_frame"] == 210
    assert round(segment["source_start"], 3) == 1.0
    assert round(segment["source_end"], 3) == 7.0


def test_materialize_broll_overlays_have_frames_and_no_overlap():
    boundary = _boundary()
    candidates = index_candidates(_material())
    selection = _valid_selection()
    assignment = _assignment(selection)
    portrait_payload = materialize_portrait_from_assignment(
        windows=_windows(boundary),
        assignment=assignment,
        candidates=candidates,
    )
    payload, drops = materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment=assignment,
        candidates=candidates,
        cut_frames=portrait_cut_frames(portrait_payload),
        enabled=True,
        max_inserts=4,
    )
    assert drops == []
    overlays = payload["overlays"]
    assert payload["enabled"] is True
    assert len(overlays) == 2
    windows_by_id = {window["window_id"]: window for window in _windows(boundary)["broll_windows"]}
    for ov, choice in zip(overlays, assignment["broll"]):
        window = windows_by_id[choice["window_id"]]
        assert ov["window_id"] == choice["window_id"]
        for key in (
            "timeline_start_frame",
            "timeline_end_frame",
            "source_start_frame",
            "source_end_frame",
        ):
            assert isinstance(ov[key], int)
        assert ov["timeline_end_frame"] > ov["timeline_start_frame"]
        assert ov["source_end_frame"] > ov["source_start_frame"]
        assert ov["timeline_start_frame"] == window["start_frame"]
        assert ov["timeline_end_frame"] == window["end_frame"]
        assert ov["timeline_end_frame"] - ov["timeline_start_frame"] == window["length_frames"]
    ordered = sorted(overlays, key=lambda o: o["timeline_start_frame"])
    assert ordered[0]["timeline_end_frame"] <= ordered[1]["timeline_start_frame"]


def test_materialize_broll_disabled_returns_empty():
    payload, drops = materialize_broll_from_assignment(
        windows=_windows(_boundary()),
        assignment=_assignment(_valid_selection()),
        candidates=index_candidates(_material()),
        cut_frames=[0, 180, 360],
        enabled=False,
        max_inserts=4,
    )
    assert drops == []
    assert payload["enabled"] is False
    assert payload["overlays"] == []


def test_agent_broll_rejects_source_shorter_than_window():
    material = _material()
    material["broll_candidates"] = [
        {
            "asset_id": "broll_flash",
            "score": 90.0,
            "metadata": {
                "clip_id": "flash_clip",
                "source_start": 0.04,
                "source_end": 1.6,
                "scene_name": "product",
            },
        }
    ]
    boundary = {
        "broll_slots": [
            {"slot_id": "bslot_flash", "start_frame": 668, "end_frame": 730}
        ]
    }
    selection = EditingSelection(
        broll=[BrollChoice(slot_id="bslot_flash", candidate_id="bc_000")]
    )

    payload, drops = materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment=_assignment(selection),
        candidates=index_candidates(material),
        cut_frames=[488, 724, 816],
        enabled=True,
        max_inserts=4,
    )

    assert payload["overlays"] == []
    assert drops == [
        {"slot_id": "bslot_flash", "candidate_id": "bc_000", "reason": "source_too_short"}
    ]


def test_agent_broll_uses_window_frames_without_repositioning():
    material = _material()
    material["broll_candidates"] = [
        {
            "asset_id": "broll_report",
            "score": 90.0,
            "metadata": {
                "clip_id": "report_clip",
                "source_start": 0.0,
                "source_end": 3.0,
                "scene_name": "report",
            },
        }
    ]
    boundary = {
        "broll_slots": [
            {"slot_id": "bslot_report", "start_frame": 81, "end_frame": 150}
        ]
    }
    selection = EditingSelection(
        broll=[BrollChoice(slot_id="bslot_report", candidate_id="bc_000")]
    )

    payload, drops = materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment=_assignment(selection),
        candidates=index_candidates(material),
        cut_frames=[0, 150],
        enabled=True,
        max_inserts=4,
    )

    assert drops == []
    [overlay] = payload["overlays"]
    assert (overlay["timeline_start_frame"], overlay["timeline_end_frame"]) == (81, 150)
    assert round(overlay["timeline_start"], 3) == 2.7
    assert round(overlay["timeline_end"], 3) == 5.0


def test_agent_broll_carries_snap_padding_without_requiring_extra_source_frames():
    material = _material()
    material["broll_candidates"] = [
        {
            "asset_id": "broll_snap",
            "score": 90.0,
            "metadata": {
                "clip_id": "snap_clip",
                "source_start": 0.0,
                "source_end": 66 / 30,
                "scene_name": "snap",
            },
        }
    ]
    boundary = {
        "broll_slots": [
            {
                "slot_id": "bslot_snap",
                "start_frame": 0,
                "end_frame": 70,
                "source_length_frames": 66,
                "pad_start": 4 / 30,
                "pad_end": 0.0,
            }
        ]
    }
    selection = EditingSelection(
        broll=[BrollChoice(slot_id="bslot_snap", candidate_id="bc_000")]
    )

    payload, drops = materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment=_assignment(selection),
        candidates=index_candidates(material),
        cut_frames=[0, 360],
        enabled=True,
        max_inserts=4,
    )

    assert drops == []
    [overlay] = payload["overlays"]
    assert (overlay["timeline_start_frame"], overlay["timeline_end_frame"]) == (0, 70)
    assert (overlay["source_start_frame"], overlay["source_end_frame"]) == (0, 66)
    assert round(overlay["pad_start"], 3) == 0.133
    assert round(overlay["pad_end"], 3) == 0.0


def test_agent_broll_uses_full_authoritative_window_length():
    material = _material()
    material["broll_candidates"] = [
        {
            "asset_id": "broll_long",
            "score": 90.0,
            "metadata": {"clip_id": "long_clip", "source_start": 0.0, "source_end": 12.0},
        },
        {
            "asset_id": "broll_short",
            "score": 80.0,
            "metadata": {"clip_id": "short_clip", "source_start": 0.0, "source_end": 1.2},
        },
    ]
    boundary = {
        "broll_slots": [
            {"slot_id": "bslot_long", "start_frame": 0, "end_frame": 300},
            {"slot_id": "bslot_short", "start_frame": 300, "end_frame": 450},
        ]
    }
    selection = EditingSelection(
        broll=[
            BrollChoice(slot_id="bslot_long", candidate_id="bc_000"),
            BrollChoice(slot_id="bslot_short", candidate_id="bc_001"),
        ]
    )

    payload, drops = materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment=_assignment(selection),
        candidates=index_candidates(material),
        cut_frames=[0, 300, 600],
        enabled=True,
        max_inserts=4,
    )

    [overlay] = payload["overlays"]
    assert round(overlay["timeline_end"] - overlay["timeline_start"], 3) == 10.0
    assert round(overlay["source_end"] - overlay["source_start"], 3) == 10.0
    assert drops == [
        {"slot_id": "bslot_short", "candidate_id": "bc_001", "reason": "source_too_short"}
    ]


def test_agent_broll_keeps_legacy_policy_argument_for_callers():
    assert materialize_broll_from_assignment.__kwdefaults__["policy"] is BROLL_GEOMETRY_POLICY


def test_same_assignment_same_frames_across_engines():
    boundary = _boundary()
    candidates = index_candidates(_material())
    assignment = _assignment(_valid_selection())
    llm_portrait = materialize_portrait_from_assignment(
        windows=_windows(boundary),
        assignment={**assignment, "engine": "editing_agent_llm"},
        candidates=candidates,
    )
    fallback_portrait = materialize_portrait_from_assignment(
        windows=_windows(boundary),
        assignment={**assignment, "engine": "deterministic_fallback"},
        candidates=candidates,
    )
    assert [
        (
            item["timeline_start_frame"],
            item["timeline_end_frame"],
            item["source_start_frame"],
            item["source_end_frame"],
        )
        for item in llm_portrait["segments"]
    ] == [
        (
            item["timeline_start_frame"],
            item["timeline_end_frame"],
            item["source_start_frame"],
            item["source_end_frame"],
        )
        for item in fallback_portrait["segments"]
    ]

    llm_broll, llm_drops = materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment={**assignment, "engine": "editing_agent_llm"},
        candidates=candidates,
        cut_frames=portrait_cut_frames(llm_portrait),
        enabled=True,
        max_inserts=4,
    )
    fallback_broll, fallback_drops = materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment={**assignment, "engine": "deterministic_fallback"},
        candidates=candidates,
        cut_frames=portrait_cut_frames(fallback_portrait),
        enabled=True,
        max_inserts=4,
    )
    assert llm_drops == fallback_drops == []
    assert [
        (
            item["timeline_start_frame"],
            item["timeline_end_frame"],
            item["source_start_frame"],
            item["source_end_frame"],
        )
        for item in llm_broll["overlays"]
    ] == [
        (
            item["timeline_start_frame"],
            item["timeline_end_frame"],
            item["source_start_frame"],
            item["source_end_frame"],
        )
        for item in fallback_broll["overlays"]
    ]


def test_editing_agent_helper_has_no_materializer_definitions():
    source = Path(_editing_agent.__file__).read_text()
    assert "def materialize_portrait" not in source
    assert "def materialize_broll" not in source
    assert "def materialize_style" not in source


def test_materialize_style_uses_chosen_font_and_bgm():
    payload, warnings, degradations = materialize_style_from_selection(
        request=_request(),
        material=_material(),
        overlay_events=[],
        font_id=_valid_selection().font_id,
        bgm_id=_valid_selection().bgm_id,
    )
    assert warnings == []
    assert degradations == []
    assert payload["font_asset_id"] == "font_yst"
    assert payload["font"]["font_id"] == "font_yst"
    assert payload["bgm"] is not None
    assert payload["bgm"]["asset_id"] == "bgm_001"
    assert payload["bgm"]["mood"] == "励志"
    assert payload["bgm"]["section_type"] == "stable_bed"


def test_materialize_style_empty_font_pool_falls_back_to_default():
    payload, warnings, degradations = materialize_style_from_selection(
        request=_request(),
        material=_material(with_font=False, with_bgm=False),
        overlay_events=[],
        font_id=None,
        bgm_id=None,
    )
    assert payload["font_asset_id"] == "case_default_font"
    assert payload["bgm"]["asset_id"] is None
    assert warnings == [
        WarningCode.font_default_used,
        WarningCode.bgm_skipped_library_unannotated,
    ]
    assert [notice.code for notice in degradations] == [
        WarningCode.bgm_skipped_library_unannotated
    ]


def test_materialize_style_bgm_disabled_yields_no_bgm():
    req = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_sandbox"},
        bgm={"enabled": False},
    )
    payload, warnings, degradations = materialize_style_from_selection(
        request=req,
        material=_material(),
        overlay_events=[],
        font_id=_valid_selection().font_id,
        bgm_id=_valid_selection().bgm_id,
    )
    assert warnings == []
    assert degradations == []
    assert payload["bgm"]["enabled"] is False
    assert payload["bgm"]["asset_id"] is None


def test_style_selection_cannot_mutate_visual_windows():
    boundary = _boundary()
    candidates = index_candidates(_material())
    assignment = _assignment(_valid_selection())
    portrait_before = materialize_portrait_from_assignment(
        windows=_windows(boundary),
        assignment=assignment,
        candidates=candidates,
    )
    broll_before, _drops = materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment=assignment,
        candidates=candidates,
        cut_frames=portrait_cut_frames(portrait_before),
        enabled=True,
        max_inserts=4,
    )
    style_payload, _warnings, _degradations = materialize_style_from_selection(
        request=_request(),
        material=_material(),
        overlay_events=[],
        font_id="font_yst",
        bgm_id="bgm_001",
    )
    assert "segments" not in style_payload
    assert "overlays" not in style_payload
    assert portrait_before == materialize_portrait_from_assignment(
        windows=_windows(boundary),
        assignment=assignment,
        candidates=candidates,
    )
    assert broll_before == materialize_broll_from_assignment(
        windows=_windows(boundary),
        assignment=assignment,
        candidates=candidates,
        cut_frames=portrait_cut_frames(portrait_before),
        enabled=True,
        max_inserts=4,
    )[0]


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


def test_materialize_broll_drops_source_shorter_than_min_insert():
    # A broll candidate shorter than the shared minimum insert duration is dropped
    # before geometry placement, so the diagnostic is source_too_short.
    material = _material()
    material["broll_candidates"] = [
        {
            "asset_id": "broll_tiny",
            "score": 80.0,
            "metadata": {"clip_id": "c", "source_start": 1.0, "source_end": 2.0},
        }
    ]
    candidates = index_candidates(material)
    selection = EditingSelection(broll=[BrollChoice(slot_id="bslot_000", candidate_id="bc_000")])
    payload, drops = materialize_broll_from_assignment(
        windows=_windows(_boundary()),
        assignment=_assignment(selection),
        candidates=candidates,
        cut_frames=[0, 180, 360],
        enabled=True,
        max_inserts=4,
    )
    assert payload["overlays"] == []
    assert drops == [
        {"slot_id": "bslot_000", "candidate_id": "bc_000", "reason": "source_too_short"}
    ]
