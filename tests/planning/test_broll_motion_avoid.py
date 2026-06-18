from __future__ import annotations

from packages.core.contracts import (
    AnnotationMetaV4,
    AnnotationV4,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    QualityEventType,
    QualityEventV4,
    UsageRole,
)
from packages.planning.material import avoid_intervals, rank_broll_candidates, subtract_bad_spans
from packages.planning.material.keywords import ScriptSegment


def _quality_event(
    event_id: str,
    event_type: QualityEventType,
    start: float,
    end: float,
    *,
    risk_tier: str = "hard",
) -> QualityEventV4:
    return QualityEventV4(
        event_id=event_id,
        event_type=event_type,
        start=start,
        end=end,
        risk_tier=risk_tier,
        confidence=0.9,
        severity=0.8,
        source="motion_guard",
    )


def _clip(segment_id: str = "cover_a", start: float = 0.0, end: float = 6.0) -> ClipV4:
    return ClipV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        semantics=ClipSemanticsV4(scene_type="工艺", narrative_role="打磨过程"),
        usage=ClipUsageV4(role=UsageRole.cover, recommended_for_voiceover=True),
        retrieval=ClipRetrievalV4(
            summary="打磨 工艺 细节",
            keywords=["打磨", "工艺"],
            retrieval_sentence="展示打磨工艺细节",
        ),
        confidence=0.9,
    )


def _annotation(
    *,
    clip: ClipV4 | None = None,
    quality_events: list[QualityEventV4] | None = None,
) -> AnnotationV4:
    chosen_clip = clip or _clip()
    return AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id="asset_broll_motion",
            case_id="case_demo",
            material_type="broll",
            duration=10.0,
        ),
        clips=[chosen_clip],
        quality_events=quality_events or [],
        quality_report={"usable_ratio": 0.9},
    )


_SEGMENTS = [ScriptSegment(text="展示打磨工艺细节", start=0.0, end=4.0, keywords=("打磨", "工艺"))]


def test_subtract_bad_spans_keeps_clean_head_for_tail_bad_span():
    assert subtract_bad_spans(0.0, 6.0, [(4.0, 7.0)], min_len=1.0) == [(0.0, 4.0)]


def test_subtract_bad_spans_splits_around_middle_bad_span():
    assert subtract_bad_spans(0.0, 6.0, [(2.0, 4.0)], min_len=1.0) == [
        (0.0, 2.0),
        (4.0, 6.0),
    ]


def test_subtract_bad_spans_drops_fully_covered_span():
    assert subtract_bad_spans(1.0, 5.0, [(0.0, 6.0)], min_len=1.0) == []


def test_subtract_bad_spans_keeps_whole_span_when_no_bad_spans():
    assert subtract_bad_spans(1.0, 5.0, [], min_len=1.0) == [(1.0, 5.0)]


def test_subtract_bad_spans_keeps_short_untouched_span_without_bad():
    # min_len only filters split remainders; an untouched clip (no overlapping
    # bad) passes through whole regardless of length, preserving the
    # pre-avoidance candidate set for clips with no quality events.
    assert subtract_bad_spans(0.0, 0.8, [], min_len=1.0) == [(0.0, 0.8)]
    assert subtract_bad_spans(0.0, 0.8, [(3.0, 4.0)], min_len=1.0) == [(0.0, 0.8)]


def test_subtract_bad_spans_drops_clean_remainders_shorter_than_min_len():
    assert subtract_bad_spans(0.0, 3.0, [(0.5, 2.6)], min_len=1.0) == []


def test_avoid_intervals_filters_to_hard_motion_and_occlusion_events_and_merges():
    annotation = _annotation(
        quality_events=[
            _quality_event("shake_a", QualityEventType.shake, 1.0, 2.0),
            _quality_event("drop_a", QualityEventType.camera_drop, 1.8, 3.0),
            _quality_event("occ_a", QualityEventType.occlusion, 4.0, 5.0),
            _quality_event("shake_soft", QualityEventType.shake, 6.0, 7.0, risk_tier="soft"),
            _quality_event("blur_hard", QualityEventType.blur, 7.0, 8.0),
            _quality_event("note_hard", QualityEventType.manual_note, 8.0, 9.0),
        ]
    )

    assert avoid_intervals(annotation) == [(1.0, 3.0), (4.0, 5.0)]


def test_rank_broll_candidates_without_quality_events_keeps_whole_clip_candidate():
    candidates = rank_broll_candidates(
        annotations={"asset_broll_motion": _annotation()},
        segments=_SEGMENTS,
    )

    assert len(candidates) == 1
    assert candidates[0].clip_id == "cover_a"
    assert candidates[0].source_start == 0.0
    assert candidates[0].source_end == 6.0


def test_rank_broll_candidates_uses_clean_head_before_tail_camera_drop():
    candidates = rank_broll_candidates(
        annotations={
            "asset_broll_motion": _annotation(
                quality_events=[
                    _quality_event("drop_tail", QualityEventType.camera_drop, 4.2, 6.0)
                ]
            )
        },
        segments=_SEGMENTS,
    )

    assert len(candidates) == 1
    assert candidates[0].clip_id == "cover_a"
    assert candidates[0].source_start == 0.0
    assert candidates[0].source_end == 4.2


def test_rank_broll_candidates_expands_middle_bad_span_into_multiple_clean_candidates():
    candidates = rank_broll_candidates(
        annotations={
            "asset_broll_motion": _annotation(
                quality_events=[_quality_event("shake_mid", QualityEventType.shake, 2.0, 4.0)]
            )
        },
        segments=_SEGMENTS,
    )

    assert [(c.clip_id, c.source_start, c.source_end) for c in candidates] == [
        ("cover_a", 0.0, 2.0),
        ("cover_a-m1", 4.0, 6.0),
    ]


def test_rank_broll_candidates_drops_clip_when_no_minimum_clean_remainder():
    candidates = rank_broll_candidates(
        annotations={
            "asset_broll_motion": _annotation(
                quality_events=[_quality_event("covered", QualityEventType.shake, 0.0, 6.0)]
            )
        },
        segments=_SEGMENTS,
    )

    assert candidates == []
