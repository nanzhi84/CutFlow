from __future__ import annotations

import pytest

from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.material import BrollCandidate, plan_coverage
from packages.planning.material.keywords import ScriptSegment


def _unit(unit_id: str, start: float, end: float, text: str = "展示补漆效果") -> NarrationUnit:
    return NarrationUnit(unit_id=unit_id, text=text, start=start, end=end, confidence=1.0)


def _candidate(
    asset_id: str,
    clip_id: str,
    *,
    score: float,
    source_start: float = 0.0,
    source_end: float = 3.0,
    keywords: tuple[str, ...] = ("补漆",),
    scene_name: str = "补漆工位",
    diversity_key: str = "工艺",
) -> BrollCandidate:
    return BrollCandidate(
        asset_id=asset_id,
        clip_id=clip_id,
        score=score,
        base_score=score,
        recency_penalty=0.0,
        matched_keywords=keywords,
        scene_name=scene_name,
        source_start=source_start,
        source_end=source_end,
        diversity_key=diversity_key,
        best_segment=ScriptSegment(text="展示补漆效果", start=0.0, end=5.0, keywords=keywords),
    )


def test_plan_coverage_fills_target_and_trims_last_segment():
    plan = plan_coverage(
        candidates=[
            _candidate("asset_a", "clip_a", score=90.0, source_start=1.0, source_end=4.0),
            _candidate("asset_b", "clip_b", score=70.0, source_start=10.0, source_end=14.0),
        ],
        units=[_unit("u1", 0.0, 5.0)],
        target_sec=5.0,
        min_segment_duration=1.0,
    )

    assert plan.sufficient is True
    assert plan.covered_sec == pytest.approx(5.0)
    assert len(plan.segments) == 2
    assert plan.segments[0].timeline_start == pytest.approx(0.0)
    assert plan.segments[0].timeline_end == pytest.approx(3.0)
    assert plan.segments[0].source_start == pytest.approx(1.0)
    assert plan.segments[0].source_end == pytest.approx(4.0)
    assert plan.segments[1].timeline_start == pytest.approx(plan.segments[0].timeline_end)
    assert plan.segments[1].timeline_end == pytest.approx(5.0)
    assert plan.segments[1].source_start == pytest.approx(10.0)
    assert plan.segments[1].source_end == pytest.approx(12.0)
    assert plan.segments[1].reason.startswith("cover full narration")


def test_plan_coverage_reports_insufficient_material_without_reusing_clips():
    plan = plan_coverage(
        candidates=[
            _candidate("asset_a", "clip_a", score=90.0, source_start=0.0, source_end=2.0),
            _candidate("asset_b", "clip_b", score=80.0, source_start=0.0, source_end=1.5),
        ],
        units=[_unit("u1", 0.0, 5.0)],
        target_sec=5.0,
        min_segment_duration=1.0,
    )

    assert plan.sufficient is False
    assert plan.covered_sec == pytest.approx(3.5)
    assert [(segment.asset_id, segment.clip_id) for segment in plan.segments] == [
        ("asset_a", "clip_a"),
        ("asset_b", "clip_b"),
    ]


def test_plan_coverage_is_deterministic_for_same_inputs():
    candidates = [
        _candidate("asset_a", "clip_a", score=90.0, source_end=2.5),
        _candidate("asset_b", "clip_b", score=70.0, source_end=2.5),
    ]
    units = [_unit("u1", 0.0, 5.0)]

    first = plan_coverage(
        candidates=candidates,
        units=units,
        target_sec=4.0,
        min_segment_duration=1.0,
    )
    second = plan_coverage(
        candidates=candidates,
        units=units,
        target_sec=4.0,
        min_segment_duration=1.0,
    )

    assert first == second


def test_plan_coverage_uses_ranked_order_so_high_relevance_clip_enters_first():
    high = _candidate("asset_high", "clip_high", score=95.0, source_end=2.0)
    low = _candidate("asset_low", "clip_low", score=40.0, source_end=2.0)

    plan = plan_coverage(
        candidates=[high, low],
        units=[_unit("u1", 0.0, 3.0)],
        target_sec=3.0,
        min_segment_duration=1.0,
    )

    assert [segment.clip_id for segment in plan.segments] == ["clip_high", "clip_low"]
    assert plan.segments[0].confidence > plan.segments[1].confidence


def test_plan_coverage_uses_freshness_seed_for_source_trim_variation():
    candidate = _candidate("asset_a", "clip_a", score=90.0, source_start=0.0, source_end=10.0)
    units = [_unit("u1", 0.0, 5.0)]

    first = plan_coverage(
        candidates=[candidate],
        units=units,
        target_sec=4.0,
        min_segment_duration=1.0,
        freshness_seed="run_a",
    )
    second = plan_coverage(
        candidates=[candidate],
        units=units,
        target_sec=4.0,
        min_segment_duration=1.0,
        freshness_seed="run_b",
    )

    assert first.sufficient is True
    assert second.sufficient is True
    assert first.segments[0].source_start != second.segments[0].source_start
    assert first.segments[0].source_end <= 10.0
    assert second.segments[0].source_end <= 10.0
