from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.contracts import (
    AnalysisWindow,
    AnnotationMetaV4,
    AnnotationV4,
    AnnotationVersion,
    ClipUsageV4,
    ClipV4,
    QualityEventType,
    QualityEventV4,
    SpeechIslandV4,
    UsageRole,
    UsageWindowV4,
    WindowReason,
)


def test_clipv4_self_corrects_duration():
    clip = ClipV4(
        segment_id="c1",
        start=0.0,
        end=2.0,
        duration=0.0,  # inconsistent -> corrected to 2.0
        usage=ClipUsageV4(role=UsageRole.main),
    )
    assert clip.duration == 2.0


def test_clipv4_rejects_zero_length():
    with pytest.raises(ValidationError):
        ClipV4(
            segment_id="c1",
            start=2.0,
            end=2.0,
            duration=0.0,
            usage=ClipUsageV4(role=UsageRole.main),
        )


def test_quality_event_validates_risk_tier():
    ev = QualityEventV4(
        event_id="e1",
        event_type=QualityEventType.shake,
        start=0.0,
        end=1.0,
        risk_tier="HARD",
    )
    assert ev.risk_tier == "hard"
    with pytest.raises(ValidationError):
        QualityEventV4(
            event_id="e2",
            event_type=QualityEventType.shake,
            start=0.0,
            end=1.0,
            risk_tier="critical",
        )


def test_annotation_v4_time_bounds_enforced():
    meta = AnnotationMetaV4(
        asset_id="a", case_id="c", material_type="portrait", duration=5.0
    )
    good_clip = ClipV4(
        segment_id="c1",
        start=0.0,
        end=5.0,
        duration=5.0,
        usage=ClipUsageV4(role=UsageRole.main),
    )
    ann = AnnotationV4(meta=meta, clips=[good_clip])
    assert ann.meta.annotation_version == AnnotationVersion.v4

    out_of_bounds = ClipV4(
        segment_id="c2",
        start=0.0,
        end=6.0,
        duration=6.0,
        usage=ClipUsageV4(role=UsageRole.main),
    )
    with pytest.raises(ValidationError):
        AnnotationV4(meta=meta, clips=[out_of_bounds])


def test_annotation_v4_unknown_duration_skips_upper_bound():
    meta = AnnotationMetaV4(
        asset_id="a", case_id="c", material_type="portrait", duration=0.0
    )
    clip = ClipV4(
        segment_id="c1",
        start=0.0,
        end=99.0,
        duration=99.0,
        usage=ClipUsageV4(role=UsageRole.main),
    )
    # duration<=0 -> no upper-bound check.
    AnnotationV4(meta=meta, clips=[clip])


def test_sensor_artifact_window_and_island_validate_time():
    AnalysisWindow(start=0.0, end=1.0, reason=WindowReason.mechanical)
    SpeechIslandV4(start=0.0, end=1.0, confidence=0.5)
    with pytest.raises(ValidationError):
        AnalysisWindow(start=1.0, end=1.0)
    with pytest.raises(ValidationError):
        SpeechIslandV4(start=1.0, end=0.5, confidence=0.5)


def test_usage_window_validates():
    UsageWindowV4(start=0.0, end=2.0, role=UsageRole.hook)
    with pytest.raises(ValidationError):
        UsageWindowV4(start=2.0, end=1.0, role=UsageRole.hook)
