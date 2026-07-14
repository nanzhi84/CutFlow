from __future__ import annotations

from packages.core.contracts import SelectionLedgerEntry
from packages.planning import material


def test_material_facade_preserves_published_compatibility_exports() -> None:
    for name in (
        "plan_coverage",
        "plan_insertions",
        "place_insertion_safely",
        "segment_script",
        "rank_portrait_clip_candidates",
        "score_simple_candidate",
    ):
        assert callable(getattr(material, name))


def test_segment_script_preserves_sentence_windows_and_keywords() -> None:
    segments = material.segment_script(
        "先展示补漆效果。再介绍门店服务！",
        keywords=["补漆", "门店"],
    )

    assert [segment.text for segment in segments] == ["先展示补漆效果", "再介绍门店服务"]
    assert segments[0].start == 0.0
    assert segments[0].end == segments[1].start
    assert segments[1].end > segments[1].start
    assert segments[0].keywords == ("补漆",)
    assert segments[1].keywords == ("门店",)


def test_score_simple_candidate_preserves_recency_demotion() -> None:
    fresh = material.score_simple_candidate(asset_id="font_a", medium_label="font")
    recent = material.score_simple_candidate(
        asset_id="font_a",
        medium_label="font",
        ledger_entries=[
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_previous",
                medium="font",
                asset_id="font_a",
                slot_phase="subtitle_font",
            )
        ],
    )

    assert fresh.base_score == 70.0
    assert fresh.recency_penalty == 0.0
    assert recent.base_score == fresh.base_score
    assert recent.recency_penalty > 0.0
    assert recent.score < fresh.score
    assert "recently used" in recent.reason
