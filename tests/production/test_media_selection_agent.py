import pytest

from packages.core.contracts import DigitalHumanVideoRequest, ErrorCode
from packages.core.contracts.artifacts import MediaSelectionAssignmentPlan
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline._media_selection_agent import (
    BrollChoice,
    MediaCandidates,
    MediaSelection,
    PortraitChoice,
    build_media_agent_input,
    deterministic_media_selection,
    index_media_candidates,
    parse_media_selection,
    repair_media_selection_to_constraints,
    select_media_with_repair,
    validate_media_selection,
)
from packages.production.pipeline._media_selection_planning import (
    _broll_assignment_limit,
    _candidate_metadata,
    _compact_prompt_input,
    _default_portrait_assignment,
    _default_portrait_payload,
    _ensure_full_coverage_broll,
    _portrait_feasibility_failure,
    _prompt_candidates_for_retrieval,
    _raw_portrait_candidate_diagnostics,
    _retrieval_topk_by_window,
    _unwrap_provider_selection,
)


def _valid_output() -> dict:
    return {
        "portrait_plan": [{"slot_id": "pslot_000", "candidate_id": "pc_000", "reason": "fit"}],
        "broll_plan": [{"slot_id": "bslot_000", "candidate_id": "bc_000", "reason": "fit"}],
        "analysis": "media only",
    }


def test_parser_accepts_only_minimal_media_schema() -> None:
    selection = parse_media_selection(_valid_output())

    assert selection.parse_errors == ()
    assert selection.overreach_fields == ()
    assert selection.portrait[0].candidate_id == "pc_000"
    assert selection.broll[0].candidate_id == "bc_000"


def test_parser_rejects_aliases_and_all_unknown_fields() -> None:
    output = _valid_output()
    output["timeline"] = []
    output["portrait_plan"][0].update(
        {"window_id": "pc_000", "source_mode": "lipsynced", "start": 0}
    )
    output["portrait_plan"][0].pop("candidate_id")
    output["broll_plan"][0].update(
        {"confidence": 0.8, "matched_keywords": ["施工"], "rect": [0, 0, 1, 1]}
    )

    selection = parse_media_selection(output)

    assert "top_level.timeline" in selection.overreach_fields
    assert "portrait_plan[0].window_id" in selection.overreach_fields
    assert "portrait_plan[0].source_mode" in selection.overreach_fields
    assert "portrait_plan[0].start" in selection.overreach_fields
    assert "broll_plan[0].confidence" in selection.overreach_fields
    assert "broll_plan[0].matched_keywords" in selection.overreach_fields
    assert "broll_plan[0].rect" in selection.overreach_fields
    assert "portrait_plan[0] missing field 'candidate_id'" in selection.parse_errors


def test_parser_reports_missing_fields_and_type_errors_for_repair() -> None:
    selection = parse_media_selection({"portrait_plan": "bad", "broll_plan": [{}], "analysis": 7})

    assert "portrait_plan must be an array" in selection.parse_errors
    assert "broll_plan[0] missing field 'slot_id'" in selection.parse_errors
    assert "analysis must be a string" in selection.parse_errors


def test_media_candidate_index_never_carries_postprocess_candidates() -> None:
    indexed = index_media_candidates(
        {
            "portrait_candidates": [{"asset_id": "portrait"}],
            "broll_candidates": [{"asset_id": "broll"}],
            "font_candidates": [{"asset_id": "font"}],
            "bgm_candidates": [{"asset_id": "bgm"}],
        }
    )

    assert indexed.portrait_by_id == {"pc_000": {"asset_id": "portrait"}}
    assert indexed.broll_by_id == {"bc_000": {"asset_id": "broll"}}
    assert not hasattr(indexed, "font_by_id")
    assert not hasattr(indexed, "bgm_by_id")


def test_provider_envelope_unwrap_is_exact() -> None:
    selection = _valid_output()

    assert _unwrap_provider_selection(selection) is selection
    assert _unwrap_provider_selection({"content": "ok", "intent": selection}) is selection
    invalid_extra = {"content": "ok", "intent": selection, "timeline": []}
    invalid_content = {"content": [], "intent": selection}
    missing_content = {"intent": selection}
    assert _unwrap_provider_selection(invalid_extra) is invalid_extra
    assert _unwrap_provider_selection(invalid_content) is invalid_content
    assert _unwrap_provider_selection(missing_content) is missing_content


def test_v2_assignment_contract_has_no_postprocess_fields() -> None:
    payload = MediaSelectionAssignmentPlan(
        engine="media_selection_agent_llm",
        portrait=[],
        broll=[],
    ).model_dump(mode="json")

    assert set(payload) == {"engine", "portrait", "broll", "diagnostics"}
    assert "font_id" not in payload
    assert "bgm_id" not in payload


def _boundary() -> dict:
    return {
        "portrait_slots": [
            {"slot_id": "p0", "start_frame": 0, "end_frame": 60},
            {"slot_id": "p1", "start_frame": 60, "end_frame": 120},
        ],
        "broll_slots": [
            {"slot_id": "b0", "start_frame": 0, "end_frame": 30},
            {"slot_id": "b1", "start_frame": 30, "end_frame": 60},
            {"slot_id": "b2", "start_frame": 60, "end_frame": 90},
        ],
    }


def _candidates() -> MediaCandidates:
    return MediaCandidates(
        portrait_by_id={
            "pc0": {
                "asset_id": "portrait_a",
                "score": 90,
                "metadata": {"clip_id": "pa", "source_start": 0.0, "source_end": 5.0},
            },
            "pc1": {
                "asset_id": "portrait_b",
                "score": 80,
                "metadata": {"clip_id": "pb", "source_start": 0.0, "source_end": 5.0},
            },
            "pc_short": {
                "asset_id": "portrait_short",
                "score": 100,
                "metadata": {"clip_id": "ps", "source_start": 0.0, "source_end": 1.0},
            },
        },
        broll_by_id={
            "bc0": {
                "asset_id": "broll_a",
                "score": 90,
                "metadata": {
                    "clip_id": "ba",
                    "source_start": 0.0,
                    "source_end": 3.0,
                    "diversity_key": "scene_a",
                },
            },
            "bc1": {
                "asset_id": "broll_b",
                "score": 80,
                "metadata": {
                    "clip_id": "bb",
                    "source_start": 0.0,
                    "source_end": 3.0,
                    "diversity_key": "scene_b",
                },
            },
            "bc2": {
                "asset_id": "broll_c",
                "score": 70,
                "metadata": {
                    "clip_id": "bc",
                    "source_start": 0.0,
                    "source_end": 3.0,
                    "diversity_key": "scene_c",
                },
            },
            "bc_short": {
                "asset_id": "broll_short",
                "score": 100,
                "metadata": {
                    "clip_id": "bs",
                    "source_start": 0.0,
                    "source_end": 0.5,
                },
            },
        },
    )


def test_validator_reports_unknown_duplicate_short_and_retrieval_violations() -> None:
    candidates = _candidates()
    selection = MediaSelection(
        portrait=[
            PortraitChoice("unknown", "pc0"),
            PortraitChoice("p0", "missing"),
            PortraitChoice("p1", "pc_short"),
            PortraitChoice("p1", "pc1"),
        ],
        broll=[
            BrollChoice("unknown", "bc0"),
            BrollChoice("b0", "missing"),
            BrollChoice("b1", "bc_short"),
            BrollChoice("b1", "bc1"),
        ],
    )

    errors = validate_media_selection(
        selection,
        boundary=_boundary(),
        candidates=candidates,
        retrieval_topk_by_window={"p1": ["pc1"], "b1": ["bc1"]},
        require_broll_coverage=True,
    )

    assert any("unknown" in error for error in errors)
    assert any("assigned more than once" in error for error in errors)
    assert any("too short" in error for error in errors)
    assert any("not legal" in error for error in errors)
    assert any("broll slots not covered" in error for error in errors)


def test_deterministic_selection_honours_topk_duration_and_diversity() -> None:
    selected = deterministic_media_selection(
        boundary=_boundary(),
        candidates=_candidates(),
        max_inserts=3,
        retrieval_topk_by_window={
            "p0": ["pc_short", "pc0"],
            "p1": ["pc0", "pc1"],
            "b0": ["bc_short", "bc0"],
            "b1": ["bc0", "bc1"],
            "b2": ["bc1", "bc2"],
        },
    )

    assert [choice.candidate_id for choice in selected.portrait] == ["pc0", "pc1"]
    assert [choice.candidate_id for choice in selected.broll] == ["bc0", "bc1", "bc2"]
    assert validate_media_selection(
        selected,
        boundary=_boundary(),
        candidates=_candidates(),
        retrieval_topk_by_window={
            "p0": ["pc_short", "pc0"],
            "p1": ["pc0", "pc1"],
            "b0": ["bc_short", "bc0"],
            "b1": ["bc0", "bc1"],
            "b2": ["bc1", "bc2"],
        },
        require_broll_coverage=True,
    ) == []


def test_local_repair_replaces_invalid_choices_and_fills_full_coverage() -> None:
    selection = MediaSelection(
        portrait=[
            PortraitChoice("p0", "pc_short", "too short"),
            PortraitChoice("p1", "pc0", "duplicate asset after repair"),
        ],
        broll=[
            BrollChoice("b0", "missing", "hallucinated"),
            BrollChoice("b1", "bc0", "would repeat"),
        ],
        analysis="repair locally",
    )
    retrieval = {
        "p0": ["pc_short", "pc0"],
        "p1": ["pc0", "pc1"],
        "b0": ["bc_short", "bc0"],
        "b1": ["bc0", "bc1"],
        "b2": ["bc1", "bc2"],
    }

    repaired, actions, errors = repair_media_selection_to_constraints(
        selection=selection,
        boundary=_boundary(),
        candidates=_candidates(),
        max_inserts=3,
        retrieval_topk_by_window=retrieval,
        require_broll_coverage=True,
    )

    assert errors == []
    assert [choice.candidate_id for choice in repaired.portrait] == ["pc0", "pc1"]
    assert [choice.candidate_id for choice in repaired.broll] == ["bc0", "bc1", "bc2"]
    assert {action["action"] for action in actions} == {"replaced", "filled"}

    overreaching = MediaSelection(overreach_fields=("timeline",))
    unchanged, no_actions, overreach_errors = repair_media_selection_to_constraints(
        selection=overreaching,
        boundary=_boundary(),
        candidates=_candidates(),
        max_inserts=3,
    )
    assert unchanged is overreaching
    assert no_actions == []
    assert any("outside the exact schema" in error for error in overreach_errors)


def test_provider_repair_loop_stops_after_valid_exact_selection() -> None:
    outputs = iter(
        [
            {"portrait_plan": [], "broll_plan": [], "analysis": "missing coverage"},
            {
                "portrait_plan": [
                    {"slot_id": "p0", "candidate_id": "pc0", "reason": "fit"},
                    {"slot_id": "p1", "candidate_id": "pc1", "reason": "fit"},
                ],
                "broll_plan": [],
                "analysis": "fixed",
            },
        ]
    )
    feedback: list[list[str]] = []

    selection, trace, errors = select_media_with_repair(
        invoke=lambda previous: (feedback.append(list(previous)), next(outputs))[1],
        boundary=_boundary(),
        candidates=_candidates(),
        max_inserts=3,
        max_repair_attempts=1,
    )

    assert errors == []
    assert selection.analysis == "fixed"
    assert len(trace) == 2
    assert feedback[0] == []
    assert any("portrait slots not covered" in error for error in feedback[1])


def test_provider_repair_loop_repairs_diversity_locally_before_reprompt() -> None:
    candidates = _candidates()
    candidates.broll_by_id["bc1"]["metadata"]["diversity_key"] = "scene_a"
    output = {
        "portrait_plan": [
            {"slot_id": "p0", "candidate_id": "pc0", "reason": "fit"},
            {"slot_id": "p1", "candidate_id": "pc1", "reason": "fit"},
        ],
        "broll_plan": [
            {"slot_id": "b0", "candidate_id": "bc0", "reason": "fit"},
            {"slot_id": "b1", "candidate_id": "bc1", "reason": "fit"},
        ],
        "analysis": "duplicate diversity",
    }
    feedback: list[list[str]] = []

    selection, trace, errors = select_media_with_repair(
        invoke=lambda previous: (feedback.append(list(previous)), output)[1],
        boundary=_boundary(),
        candidates=candidates,
        max_inserts=3,
        max_repair_attempts=1,
    )

    assert errors == []
    assert feedback == [[]]
    assert [choice.candidate_id for choice in selection.broll] == ["bc0", "bc2"]
    assert trace[0]["errors"] == [
        "broll diversity_key 'scene_a' is assigned more than once"
    ]
    assert trace[1]["attempt"] == "local_media_constraint_repair"
    assert trace[1]["provider_attempt"] == 0
    assert trace[1]["error_count"] == 0


def test_media_planning_utilities_keep_retrieval_and_defaults_media_only() -> None:
    retrieval = _retrieval_topk_by_window(
        {
            "candidates_by_window": {
                "p0": [{"candidate_id": "pc0"}, {"candidate_id": ""}, "bad"],
                "b0": [{"candidate_id": "bc0"}],
                "ignored": "not-a-list",
            }
        }
    )
    assert retrieval == {"p0": ["pc0"], "b0": ["bc0"]}
    assert _retrieval_topk_by_window({"candidates_by_window": []}) == {}
    filtered = _prompt_candidates_for_retrieval(_candidates(), retrieval)
    assert set(filtered.portrait_by_id) == {"pc0"}
    assert set(filtered.broll_by_id) == {"bc0"}

    compact = _compact_prompt_input(
        {
            "script": "脚本",
            "portrait_slots": [
                "bad",
                {
                    "slot_id": "p0",
                    "required_seconds": 2.0,
                    "legal_candidate_ids": ["pc0", "pc1"],
                    "retrieval_topk_candidate_ids": ["pc1", "missing"],
                },
            ],
            "broll_slots": [
                "bad",
                {
                    "slot_id": "b0",
                    "required_seconds": 1.0,
                    "text": "施工",
                    "legal_candidate_ids": ["bc0"],
                    "retrieval_topk_candidate_ids": ["bc_short", "bc0"],
                },
            ],
            "portrait_candidates": [],
            "broll_candidates": [
                {"candidate_id": "bc0", "asset_id": "broll_a"},
                {"candidate_id": "bc_short", "asset_id": "broll_short"},
            ],
        }
    )
    assert compact["portrait_slots"][0]["legal_candidate_ids"] == ["pc1"]
    assert compact["broll_slots"][0]["legal_candidate_ids"] == ["bc0"]
    assert "bc0" in compact["broll_candidates"]
    assert "bc_short" not in compact["broll_candidates"]
    assert "allowed_slot_ids" not in compact["broll_candidates"]

    windows = {
        "portrait_windows": [{"window_id": "p0"}],
        "default_assignment": {
            "portrait": [
                {"window_id": "pc0", "segment_payload": {"source_mode": "lipsynced"}}
            ],
            "portrait_plan_payload": {"enabled": True, "segments": []},
        },
    }
    assert _default_portrait_assignment(windows)[0]["candidate_id"] == "pc0"
    assert _default_portrait_payload(windows) == {"enabled": True, "segments": []}
    assert _candidate_metadata(None) == {}
    assert _candidate_metadata({"metadata": {"scene": "工地"}}) == {"scene": "工地"}


def test_broll_prompt_legal_candidates_intersect_capacity_and_retrieval_topk() -> None:
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_demo"},
        broll={"enabled": True, "mode": "insert", "max_inserts": 1},
    )
    agent_input = build_media_agent_input(
        request=request,
        boundary={
            "portrait_slots": [],
            "broll_slots": [
                {
                    "slot_id": "b0",
                    "start_frame": 0,
                    "end_frame": 30,
                    "text": "施工",
                }
            ],
        },
        candidates=_candidates(),
        narration_units=[],
        duration=1.0,
        retrieval_topk_by_window={"b0": ["bc_short", "bc1", "missing"]},
    )

    assert agent_input["broll_slots"][0]["legal_candidate_ids"] == ["bc0", "bc1", "bc2"]
    compact = _compact_prompt_input(agent_input)
    assert compact["broll_slots"] == [
        {
            "slot_id": "b0",
            "required_seconds": 1.0,
            "text": "施工",
            "legal_candidate_ids": ["bc1"],
        }
    ]
    assert "bc1" in compact["broll_candidates"]
    assert "bc_short" not in compact["broll_candidates"]


def test_media_planning_feasibility_and_full_coverage_failures_are_explicit() -> None:
    failure = _portrait_feasibility_failure(
        {
            "portrait_slots": [
                "bad",
                {
                    "slot_id": "p0",
                    "required_frames": 60,
                    "legal_candidate_ids": ["pc0"],
                    "retrieval_topk_candidate_ids": [],
                },
            ]
        }
    )
    assert failure == {
        "failed_slot_ids": ["p0"],
        "required_frames_by_slot": {"p0": 60},
    }
    diagnostics = _raw_portrait_candidate_diagnostics(
        {"portrait_candidates": list(_candidates().portrait_by_id.values())}
    )
    assert diagnostics["portrait_candidate_count"] == 3
    assert diagnostics["longest_available_source_frames"] == 150

    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_demo"},
        broll={"enabled": True, "mode": "full_coverage", "max_inserts": 1},
    )
    windows = {"broll_windows": [{"window_id": "b0"}, "bad"]}
    assert _broll_assignment_limit(request=request, windows=windows) == 1
    with pytest.raises(NodeExecutionError) as exc:
        _ensure_full_coverage_broll(
            windows=windows,
            broll_payload={"overlays": []},
            broll_drops=[{"slot_id": "b0"}],
        )
    assert exc.value.error.code == ErrorCode.material_insufficient_broll
