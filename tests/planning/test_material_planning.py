"""Unit tests for the real material-planning domain (packages/planning).

Fixture AnnotationV4 + NarrationUnits prove:
  (a) real keyword matching picks the relevant clip, scores differ (not all 1),
      and insertion points land inside real narration windows (not 0/3/6/9...);
  (b) recency: a clip picked in run 1 is demoted in run 2 via the ledger;
  (c) insufficiency / no annotation -> empty result (honest soft-degrade upstream),
      never a fabricated pick.
"""

from __future__ import annotations

from packages.core.contracts import (
    AnnotationMetaV4,
    AnnotationV4,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    SelectionLedgerEntry,
    UsageRole,
    UsageWindowV4,
)
from packages.core.contracts.artifacts import NarrationUnit
from packages.planning.material import (
    demote_recent_broll_candidates,
    extract_keywords,
    plan_insertions,
    rank_broll_candidates,
    rank_portrait_clip_candidates,
)
from packages.planning.material.broll_pack import BrollCandidate
from packages.planning.material.keywords import ScriptSegment


def _clip(
    segment_id,
    start,
    end,
    keywords,
    *,
    scene_type="场景",
    role=UsageRole.cover,
    narrative_role="片段",
    lip_sync=False,
):
    return ClipV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        semantics=ClipSemanticsV4(scene_type=scene_type, narrative_role=narrative_role),
        usage=ClipUsageV4(role=role, recommended_for_lip_sync=lip_sync),
        retrieval=ClipRetrievalV4(
            summary=" ".join(keywords),
            keywords=list(keywords),
            retrieval_sentence=" ".join(keywords),
        ),
    )


def _annotation(asset_id, clips, *, duration=10.0, usable_ratio=0.9):
    return AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id=asset_id,
            case_id="case_demo",
            material_type="broll",
            duration=duration,
        ),
        clips=clips,
        usage_windows=[
            UsageWindowV4(start=c.start, end=c.end, role=UsageRole.cover, confidence=0.9)
            for c in clips
        ],
        quality_report={"usable_ratio": usable_ratio},
    )


def _narration_segments(units):
    return [
        ScriptSegment(
            text=u.text,
            start=u.start,
            end=u.end,
            keywords=tuple(extract_keywords(u.text)),
        )
        for u in units
    ]


def _units():
    return [
        NarrationUnit(
            unit_id="u1", text="先讲解打磨工艺的细节。", start=0.0, end=4.0, confidence=1.0
        ),
        NarrationUnit(
            unit_id="u2", text="再展示补漆效果对比。", start=4.0, end=8.0, confidence=1.0
        ),
    ]


def test_real_matching_picks_keyword_relevant_clip_with_distinct_scores():
    units = _units()
    segments = _narration_segments(units)
    # Two assets: one keyword-relevant (补漆/效果), one unrelated (美食/餐厅).
    annotations = {
        "asset_relevant": _annotation(
            "asset_relevant", [_clip("rel_1", 0.0, 4.0, ["补漆", "效果", "对比"])]
        ),
        "asset_unrelated": _annotation(
            "asset_unrelated",
            [
                _clip(
                    "unrel_1",
                    0.0,
                    4.0,
                    ["美食", "餐厅", "菜品"],
                    scene_type="餐饮",
                    narrative_role="餐厅环境",
                )
            ],
        ),
    }
    candidates = rank_broll_candidates(annotations=annotations, segments=segments)

    # The relevant clip is ranked first and the unrelated clip is filtered out by
    # the relevance floor (it never matched any narration beat).
    assert candidates, "a keyword-relevant clip must be offered"
    assert candidates[0].asset_id == "asset_relevant"
    assert "asset_unrelated" not in {c.asset_id for c in candidates}
    # Real scores: keyword-relevant clip scores well above the seed's flat 1.
    assert candidates[0].score > 1.0
    assert candidates[0].matched_keywords  # real overlap surfaced


def test_distinct_clips_get_distinct_scores_not_all_one():
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_a": _annotation(
            "asset_a",
            [
                _clip("a_strong", 0.0, 4.0, ["补漆", "效果", "对比"]),
                _clip("a_weak", 4.0, 8.0, ["打磨"], scene_type="工艺"),
            ],
        )
    }
    candidates = rank_broll_candidates(annotations=annotations, segments=segments)
    scores = sorted({c.score for c in candidates})
    assert len(scores) >= 2, "different clips must produce different real scores"
    assert scores != [1.0]


def test_insertion_points_land_in_real_narration_windows():
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_a": _annotation("asset_a", [_clip("a1", 0.0, 4.0, ["补漆", "效果"])]),
    }
    candidates = rank_broll_candidates(annotations=annotations, segments=segments)
    insertions = plan_insertions(candidates=candidates, units=units, max_inserts=2)

    assert insertions
    for ins in insertions:
        # Each insert starts inside a real narration window — never the old
        # mechanical 0/3/6/9 grid.
        assert any(u.start <= ins.timeline_start < u.end for u in units)
        assert ins.timeline_end <= max(u.end for u in units)
    starts = [ins.timeline_start for ins in insertions]
    assert starts != [0.0, 3.0][: len(starts)]


def test_broll_candidate_and_insert_carry_diversity_key_for_recency():
    # The diversity cluster (scene_type / narrative_role) must be carried from the
    # ranked candidate through to the insertion so the selection ledger can persist
    # it and cluster-level recency demotion stops being dead in practice.
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_a": _annotation(
            "asset_a", [_clip("a1", 0.0, 4.0, ["补漆", "效果"], scene_type="补漆台")]
        ),
    }
    candidates = rank_broll_candidates(annotations=annotations, segments=segments)
    assert candidates
    assert candidates[0].diversity_key == "补漆台"
    insertions = plan_insertions(candidates=candidates, units=units, max_inserts=2)
    assert insertions
    assert insertions[0].diversity_key == "补漆台"


def test_broll_insert_never_spills_past_a_short_narration_beat():
    # A timeline whose middle beat [4.0, 4.8] is shorter than _MIN_INSERT_SECONDS.
    units = [
        NarrationUnit(
            unit_id="u1", text="先讲解打磨工艺的细节。", start=0.0, end=4.0, confidence=1.0
        ),
        NarrationUnit(unit_id="u2", text="补漆。", start=4.0, end=4.8, confidence=1.0),
        NarrationUnit(
            unit_id="u3", text="再展示补漆效果对比的整体呈现。", start=4.8, end=8.0, confidence=1.0
        ),
    ]
    # A candidate matched to the short 0.8s beat. A real per-clause TTS unit is
    # frequently sub-1.5s, so this path is hit in practice.
    short_beat = ScriptSegment(text="补漆。", start=4.0, end=4.8, keywords=("补漆",))
    candidate = BrollCandidate(
        asset_id="asset_a",
        clip_id="a1",
        score=50.0,
        base_score=50.0,
        recency_penalty=0.0,
        matched_keywords=("补漆",),
        scene_name="补漆",
        source_start=0.0,
        source_end=4.0,
        best_segment=short_beat,
    )
    insertions = plan_insertions(candidates=[candidate], units=units, max_inserts=2)

    # Invariant: an insert must never extend past the narration beat it is anchored
    # to (no overlay bleeding into the next spoken window).
    for ins in insertions:
        host = next(u for u in units if u.start <= ins.timeline_start < u.end)
        assert ins.timeline_end <= host.end, (
            f"insert {ins.timeline_start}-{ins.timeline_end} spills past beat {host.start}-{host.end}"
        )
    # The sole candidate's beat is too short to host a minimum-length insert, so it
    # is honestly dropped rather than clamped up and spilled.
    assert insertions == []


def test_broll_insertions_use_freshness_seed_for_new_timing_and_trim():
    units = [
        NarrationUnit(
            unit_id="u1", text="展示门店货架和热销商品。", start=0.0, end=7.0, confidence=1.0
        )
    ]
    beat = ScriptSegment(text=units[0].text, start=0.0, end=7.0, keywords=("门店", "商品"))
    candidate = BrollCandidate(
        asset_id="asset_a",
        clip_id="clip_a",
        score=90.0,
        base_score=90.0,
        recency_penalty=0.0,
        matched_keywords=("门店",),
        scene_name="货架",
        source_start=0.0,
        source_end=9.0,
        best_segment=beat,
    )

    first = plan_insertions(
        candidates=[candidate],
        units=units,
        max_inserts=1,
        freshness_seed="run_a",
    )
    second = plan_insertions(
        candidates=[candidate],
        units=units,
        max_inserts=1,
        freshness_seed="run_b",
    )

    assert first and second
    assert (
        first[0].timeline_start,
        first[0].source_start,
    ) != (
        second[0].timeline_start,
        second[0].source_start,
    )
    assert first[0].timeline_end <= units[0].end
    assert second[0].timeline_end <= units[0].end


def test_recency_demotes_clip_picked_in_previous_run():
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_a": _annotation("asset_a", [_clip("a1", 0.0, 4.0, ["补漆", "效果"])]),
        "asset_b": _annotation(
            "asset_b", [_clip("b1", 0.0, 4.0, ["补漆", "效果"], scene_type="场景B")]
        ),
    }

    # Run 1: no ledger history -> tie broken deterministically by id (asset_a).
    run1 = rank_broll_candidates(annotations=annotations, segments=segments, ledger_entries=[])
    winner = run1[0]
    assert winner.recency_penalty == 0.0

    # Run 2: the run-1 winner was recorded in the ledger -> it is demoted below
    # the fresh alternative on the next run.
    ledger = [
        SelectionLedgerEntry(
            case_id="case_demo",
            run_id="run_1",
            medium="broll",
            asset_id=winner.asset_id,
            slot_phase="broll_1",
        )
    ]
    run2 = rank_broll_candidates(annotations=annotations, segments=segments, ledger_entries=ledger)
    run2_by_asset = {c.asset_id: c for c in run2}
    assert run2_by_asset[winner.asset_id].recency_penalty > 0.0
    assert run2[0].asset_id != winner.asset_id  # fresh clip now ranks first


def test_broll_recent_exact_clip_is_hard_ranked_after_fresh_clip():
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_recent": _annotation(
            "asset_recent",
            [_clip("recent_clip", 0.0, 8.0, ["补漆", "效果", "对比", "打磨"])],
            duration=8.0,
            usable_ratio=1.0,
        ),
        "asset_fresh": _annotation(
            "asset_fresh",
            [_clip("fresh_clip", 0.0, 4.0, ["补漆"], scene_type="新鲜场景")],
            usable_ratio=0.5,
        ),
    }
    ledger = [
        SelectionLedgerEntry(
            case_id="case_demo",
            run_id="run_1",
            medium="broll",
            asset_id="asset_recent",
            clip_id="recent_clip",
            slot_phase="broll_1",
        )
    ]

    ranked = rank_broll_candidates(annotations=annotations, segments=segments, ledger_entries=ledger)

    assert ranked[0].asset_id == "asset_fresh"
    assert ranked[-1].asset_id == "asset_recent"


def test_portrait_clip_recency_demotes_recently_used_portrait():
    annotations = {
        "p_fresh": _annotation(
            "p_fresh",
            [_clip("fresh_talk", 0.0, 15.0, ["口播"], role=UsageRole.main, lip_sync=True)],
            duration=15.0,
        ),
        "p_used": _annotation(
            "p_used",
            [_clip("used_talk", 0.0, 15.0, ["口播"], role=UsageRole.main, lip_sync=True)],
            duration=15.0,
        ),
    }
    candidates = rank_portrait_clip_candidates(
        annotations=annotations,
        ledger_entries=[
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_1",
                medium="portrait",
                asset_id="p_used",
                slot_phase="portrait_main",
            )
        ],
    )
    by_asset = {candidate.asset_id: candidate for candidate in candidates}
    assert by_asset["p_used"].clip_id == "used_talk"
    assert by_asset["p_fresh"].clip_id == "fresh_talk"
    assert by_asset["p_used"].source_start == 0.0
    assert by_asset["p_used"].source_end == 15.0
    assert by_asset["p_used"].recency_penalty > 0.0
    assert by_asset["p_used"].score < by_asset["p_fresh"].score


def test_demote_recent_broll_with_empty_penalties_is_a_noop_over_empty_ledger_ranking():
    # The b-roll planning nodes rank against an EMPTY ledger and then re-apply
    # MaterialPack's recency penalties. With no recorded penalties this is a pure
    # no-op: same scores, same order as the empty-ledger ranking.
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_a": _annotation("asset_a", [_clip("a1", 0.0, 4.0, ["补漆", "效果"])]),
        "asset_b": _annotation(
            "asset_b", [_clip("b1", 0.0, 4.0, ["补漆", "效果"], scene_type="场景B")]
        ),
    }
    ranked = rank_broll_candidates(annotations=annotations, segments=segments, ledger_entries=())
    demoted = demote_recent_broll_candidates(ranked, penalty_by_clip={})

    assert [(c.asset_id, c.clip_id, c.score, c.recency_penalty) for c in demoted] == [
        (c.asset_id, c.clip_id, c.score, c.recency_penalty) for c in ranked
    ]


def test_demote_recent_broll_applies_material_pack_penalty_to_score_and_order():
    # A penalty MaterialPack computed for one cluster demotes that clip's score AND
    # ranks it behind the fresh alternative — reproducing the ledger demotion without
    # the planning node reading the ledger.
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_a": _annotation("asset_a", [_clip("a1", 0.0, 4.0, ["补漆", "效果"])]),
        "asset_b": _annotation(
            "asset_b", [_clip("b1", 0.0, 4.0, ["补漆", "效果"], scene_type="场景B")]
        ),
    }
    ranked = rank_broll_candidates(annotations=annotations, segments=segments, ledger_entries=())
    fresh_first = ranked[0]  # equal base scores -> tie broken deterministically by id
    demoted = demote_recent_broll_candidates(
        ranked,
        penalty_by_clip={},
        penalty_by_diversity={(fresh_first.asset_id, fresh_first.diversity_key): 0.5},
    )

    by_asset = {c.asset_id: c for c in demoted}
    assert by_asset[fresh_first.asset_id].recency_penalty == 0.5
    assert by_asset[fresh_first.asset_id].score < fresh_first.score
    # The demoted clip is no longer ranked first; the untouched (fresh) clip wins.
    assert demoted[0].asset_id != fresh_first.asset_id


def test_no_annotations_yields_no_broll_candidates():
    units = _units()
    segments = _narration_segments(units)
    candidates = rank_broll_candidates(annotations={}, segments=segments)
    assert candidates == []
    assert plan_insertions(candidates=candidates, units=units, max_inserts=2) == []


def test_unrelated_annotation_yields_no_candidate_not_a_fake_pick():
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_x": _annotation(
            "asset_x",
            [
                _clip(
                    "x1",
                    0.0,
                    4.0,
                    ["航天", "火箭", "卫星"],
                    scene_type="科技",
                    narrative_role="发射现场",
                )
            ],
        )
    }
    candidates = rank_broll_candidates(annotations=annotations, segments=segments)
    assert candidates == []  # honest: no relevance -> no pick (never fabricated)


def test_avoid_role_clip_is_never_offered():
    units = _units()
    segments = _narration_segments(units)
    annotations = {
        "asset_a": _annotation(
            "asset_a", [_clip("a1", 0.0, 4.0, ["补漆", "效果"], role=UsageRole.avoid)]
        )
    }
    candidates = rank_broll_candidates(annotations=annotations, segments=segments)
    assert candidates == []
