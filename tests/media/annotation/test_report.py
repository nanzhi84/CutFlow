from __future__ import annotations

from packages.core.contracts import (
    ClipUsageV4,
    ClipV4,
    QualityEventV4,
    UsageRole,
)
from packages.media.annotation import build_quality_report, merged_event_duration


def test_merged_event_duration_union():
    # [0,2] + [1,3] overlap -> 3; touching [3,4] merges -> total 4.
    assert merged_event_duration([(0.0, 2.0), (1.0, 3.0), (3.0, 4.0)]) == 4.0
    # Containment takes outer.
    assert merged_event_duration([(0.0, 10.0), (3.0, 5.0)]) == 10.0
    # Zero-length / reversed ignored.
    assert merged_event_duration([(1.0, 1.0), (2.0, 1.0)]) == 0.0


def test_build_quality_report_unknown_material_type_is_empty():
    assert build_quality_report(
        material_type="mystery", duration=10.0, clips=[], quality_events=[]
    ) == {}


def test_build_broll_report_from_typed_models():
    clips = [
        ClipV4(
            segment_id="c1",
            start=0.0,
            end=5.0,
            duration=5.0,
            usage=ClipUsageV4(role=UsageRole.cover),
        ),
        ClipV4(
            segment_id="c2",
            start=5.0,
            end=10.0,
            duration=5.0,
            usage=ClipUsageV4(role=UsageRole.avoid),
        ),
    ]
    events = [
        QualityEventV4(
            event_id="e1",
            event_type="shake",
            start=1.0,
            end=2.0,
            risk_tier="hard",
        )
    ]
    report = build_quality_report(
        material_type="scenery", duration=10.0, clips=clips, quality_events=events
    )
    # One non-avoid clip (0-5) -> usable_ratio 0.5.
    assert report["usable_ratio"] == 0.5
    # 1s shake out of 10s -> stability 90.
    assert report["stability_score"] == 90.0
    assert report["hard_quality_count"] == 1
    assert report["soft_quality_count"] == 1


def test_build_portrait_report_clean_clip():
    clips = [
        ClipV4(
            segment_id="c1",
            start=0.0,
            end=10.0,
            duration=10.0,
            usage=ClipUsageV4(role=UsageRole.main, recommended_for_lip_sync=True),
        )
    ]
    report = build_quality_report(
        material_type="portrait", duration=10.0, clips=clips, quality_events=[]
    )
    assert report["speech_stability"] == "stable"
    assert report["tail_state"] == "clean"
    assert 0 <= report["lip_sync_suitability_score"] <= 100


def test_manual_note_excluded_from_risk_aggregation():
    clips = [
        ClipV4(
            segment_id="c1",
            start=0.0,
            end=10.0,
            duration=10.0,
            usage=ClipUsageV4(role=UsageRole.cover),
        )
    ]
    events = [
        QualityEventV4(
            event_id="m1",
            event_type="manual_note",
            start=1.0,
            end=2.0,
            risk_tier="hard",
        )
    ]
    report = build_quality_report(
        material_type="scenery", duration=10.0, clips=clips, quality_events=events
    )
    assert report["hard_quality_count"] == 0
    assert report["stability_score"] == 100.0


def test_build_report_accepts_plain_dicts():
    clips = [
        {
            "start": 0.0,
            "end": 4.0,
            "usage": {"role": "cover"},
            "semantics": {"scene_type": "kitchen"},
            "visual": {"shot_scale": "wide"},
        }
    ]
    report = build_quality_report(
        material_type="broll", duration=4.0, clips=clips, quality_events=[]
    )
    assert report["dominant_scene_types"] == ["kitchen"]
    assert report["dominant_shot_scales"] == ["wide"]
