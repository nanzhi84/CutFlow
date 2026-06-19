import pytest

from packages.core import contracts as c


def test_bgm_segment_valid_and_derives_duration():
    segment = c.BgmSegmentV4(
        segment_id="seg_1",
        start=10.0,
        end=70.0,
        duration=0.0,
        role="climax",
        drop_anchor_sec=38.0,
        energy=0.72,
        mood="燃",
        scene_fit=["高光混剪"],
        source="sensor+audio",
    )

    assert segment.duration == 60.0
    assert segment.role == c.BgmSegmentRole.climax


def test_annotation_v4_bgm_segments_bounds_enforced():
    meta = c.AnnotationMetaV4(
        asset_id="bgm",
        case_id="case",
        material_type="bgm",
        duration=90.0,
    )

    with pytest.raises(ValueError, match="bgm_segment"):
        c.AnnotationV4(
            meta=meta,
            bgm_segments=[
                c.BgmSegmentV4(
                    segment_id="bad",
                    start=80.0,
                    end=95.0,
                    duration=15.0,
                )
            ],
        )


def test_annotation_v4_has_no_bgm_usage_windows_field():
    fields = c.AnnotationV4.model_fields

    assert "bgm_segments" in fields
    assert "bgm_usage_windows" not in fields
    assert not hasattr(c, "BgmUsageWindowV4")
