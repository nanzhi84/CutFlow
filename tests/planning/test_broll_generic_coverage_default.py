"""Generic b-roll coverage on the DEFAULT (digital_human_v2) path.

The default pipeline should be able to use person-free "clean cover" clips that
have NO keyword overlap with the narration as sparse b-roll fillers, drawn from
the clean pool and sprinkled across the timeline — rather than soft-degrading to
empty b-roll just because no clip literally shares a jieba keyword with the
script.

These tests pin the two halves of that behaviour:
  (A1) generic candidates must NOT all collapse onto the first narration beat
       (the matching.py +0.05 duration-fit bonus must not anchor them), so a
       no-overlap generic candidate carries ``best_segment is None``;
  (A2) ``plan_insertions`` must SPREAD anchorless generic candidates across
       distinct narration units instead of stalling on the first window.

The person filter, the relevance floor itself, and keyword-matched anchoring are
unchanged and covered elsewhere (test_broll_person_exclusion / test_material_planning).
"""

from __future__ import annotations

from packages.core.contracts import (
    AnnotationMetaV4,
    AnnotationV4,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    UsageRole,
)
from packages.core.contracts.artifacts import NarrationUnit
from packages.core.contracts.jobs import BrollOptions
from packages.planning.material import extract_keywords, plan_insertions, rank_broll_candidates
from packages.planning.material.keywords import ScriptSegment


def _clean_clip(segment_id, start, end, *, keywords, subject_type="interior_room"):
    """A person-free scene/cover clip (passes avoid/lip-sync/person gates)."""
    return ClipV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        semantics=ClipSemanticsV4(scene_type="场景", subject_type=subject_type),
        usage=ClipUsageV4(role=UsageRole.cover, recommended_for_lip_sync=False),
        retrieval=ClipRetrievalV4(
            summary=" ".join(keywords),
            keywords=list(keywords),
            retrieval_sentence=" ".join(keywords),
        ),
        confidence=0.9,
    )


def _video_annotation(asset_id, clips):
    return AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id=asset_id, case_id="case_demo", material_type="video", duration=60.0
        ),
        clips=clips,
        quality_report={"usable_ratio": 0.9},
    )


# Narration whose keywords ("服务"/"团队"...) never overlap the clips' keywords
# ("窗外"/"绿植"...): every clip falls through the relevance floor into generic.
def _units():
    return [
        NarrationUnit(unit_id="u1", text="今天聊聊我们的服务理念。", start=0.0, end=4.0, confidence=1.0),
        NarrationUnit(unit_id="u2", text="团队一直在打磨流程。", start=4.0, end=8.0, confidence=1.0),
        NarrationUnit(unit_id="u3", text="也很重视长期口碑。", start=8.0, end=12.0, confidence=1.0),
        NarrationUnit(unit_id="u4", text="欢迎大家多多了解。", start=12.0, end=16.0, confidence=1.0),
    ]


def _segments(units):
    return [ScriptSegment(text=u.text, start=u.start, end=u.end, keywords=()) for u in units]


def test_brolloptions_allows_generic_coverage_by_default():
    # Contract default: the new knob is ON unless explicitly disabled.
    assert BrollOptions().allow_generic_coverage is True


def test_clean_no_overlap_clip_is_skipped_without_generic_but_admitted_with_it():
    units = _units()
    segments = _segments(units)
    annotation = _video_annotation("vid", [_clean_clip("c1", 0.0, 4.0, keywords=("窗外", "绿植"))])

    # Default (floor on, no generic): a clean clip with zero keyword overlap is
    # honestly dropped.
    assert rank_broll_candidates(annotations={"vid": annotation}, segments=segments) == []

    # With generic coverage: it becomes a candidate, with no matched keywords.
    cands = rank_broll_candidates(
        annotations={"vid": annotation}, segments=segments, include_generic_coverage=True
    )
    assert len(cands) == 1
    assert cands[0].matched_keywords == ()


def test_generic_candidates_do_not_all_anchor_to_the_first_beat():
    # A1: the +0.05 duration-fit bonus must NOT pin every no-overlap clip to the
    # same first narration beat. A generic (no real overlap) candidate carries no
    # anchor, so plan_insertions is free to distribute it.
    units = _units()
    segments = _segments(units)
    annotation = _video_annotation(
        "vid",
        [
            _clean_clip("c1", 0.0, 3.0, keywords=("窗外", "绿植")),
            _clean_clip("c2", 3.0, 6.0, keywords=("木纹", "桌面")),
            _clean_clip("c3", 6.0, 9.0, keywords=("灯光", "氛围")),
        ],
    )
    cands = rank_broll_candidates(
        annotations={"vid": annotation}, segments=segments, include_generic_coverage=True
    )
    assert cands
    assert all(c.best_segment is None for c in cands), (
        "no-overlap generic candidates must not be anchored to a beat by the "
        "duration-fit tie-breaker"
    )


def test_generic_inserts_spread_across_distinct_narration_units():
    # A2: anchorless generic candidates must be sprinkled across DIFFERENT
    # narration units, not stalled on the first window.
    units = _units()
    segments = _segments(units)
    annotation = _video_annotation(
        "vid",
        [
            _clean_clip("c1", 0.0, 3.0, keywords=("窗外", "绿植")),
            _clean_clip("c2", 3.0, 6.0, keywords=("木纹", "桌面")),
            _clean_clip("c3", 6.0, 9.0, keywords=("灯光", "氛围")),
            _clean_clip("c4", 9.0, 12.0, keywords=("陈列", "样板")),
        ],
    )
    cands = rank_broll_candidates(
        annotations={"vid": annotation}, segments=segments, include_generic_coverage=True
    )
    insertions = plan_insertions(candidates=cands, units=units, max_inserts=4)

    assert len(insertions) >= 2, "generic clips should yield multiple sprinkled inserts"
    host_units = {
        next(u.unit_id for u in units if u.start <= ins.timeline_start < u.end)
        for ins in insertions
    }
    assert len(host_units) >= 2, "inserts must spread across distinct narration units"
    # Invariant preserved: an insert never spills past its host beat.
    for ins in insertions:
        host = next(u for u in units if u.start <= ins.timeline_start < u.end)
        assert ins.timeline_end <= host.end


def test_generics_fill_earlier_windows_even_when_a_match_anchors_late():
    # Regression for the two-phase placement: a keyword match anchored to the LAST
    # window must NOT suppress generic fillers in the earlier empty windows. A
    # single-pass cursor would jump to the late match and drop every earlier
    # generic; the two-phase fill places generics per still-empty window regardless.
    units = [
        NarrationUnit(unit_id="u1", text="开场先随便说两句。", start=0.0, end=4.0, confidence=1.0),
        NarrationUnit(unit_id="u2", text="中间也聊点别的事。", start=4.0, end=8.0, confidence=1.0),
        NarrationUnit(unit_id="u3", text="然后继续说些内容。", start=8.0, end=12.0, confidence=1.0),
        NarrationUnit(unit_id="u4", text="最后讲解打磨工艺的细节。", start=12.0, end=16.0, confidence=1.0),
    ]
    segments = [
        ScriptSegment(text=u.text, start=u.start, end=u.end, keywords=tuple(extract_keywords(u.text)))
        for u in units
    ]
    annotation = _video_annotation(
        "vid",
        [
            _clean_clip("kw_late", 0.0, 3.0, keywords=("打磨", "工艺")),  # matches u4 only
            _clean_clip("gen1", 3.0, 6.0, keywords=("窗外", "绿植")),
            _clean_clip("gen2", 6.0, 9.0, keywords=("木纹", "桌面")),
            _clean_clip("gen3", 9.0, 12.0, keywords=("灯光", "氛围")),
        ],
    )
    cands = rank_broll_candidates(
        annotations={"vid": annotation}, segments=segments, include_generic_coverage=True
    )
    insertions = plan_insertions(candidates=cands, units=units, max_inserts=4)
    clip_ids = {ins.clip_id for ins in insertions}
    host_units = {
        next(u.unit_id for u in units if u.start <= ins.timeline_start < u.end)
        for ins in insertions
    }
    assert "kw_late" in clip_ids, "the keyword match must be placed (Phase 1)"
    assert "u4" in host_units, "the match anchors in the last window"
    # generics still fill the earlier empty windows instead of being suppressed
    assert len(host_units & {"u1", "u2", "u3"}) >= 2
