from packages.core.contracts.artifacts import MediaSelectionAssignmentPlan
from packages.production.pipeline._media_selection_agent import (
    index_media_candidates,
    parse_media_selection,
)
from packages.production.pipeline._media_selection_planning import _unwrap_provider_selection


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
