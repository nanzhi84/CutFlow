import pytest

from packages.core import contracts as c


def test_bgm_usage_window_valid():
    w = c.BgmUsageWindowV4(
        segment_id="w1",
        start=45.0,
        end=75.0,
        duration=30.0,
        role=c.BgmSegmentRole.climax,
        drop_anchor_sec=58.0,
        energy=0.8,
        mood="燃",
        scene_fit=["产品高光", "结尾收束"],
        reason="副歌高潮",
        confidence=0.9,
        source="sensor+audio",
    )
    assert w.role == c.BgmSegmentRole.climax
    assert w.drop_anchor_sec == 58.0


def test_bgm_usage_window_end_must_exceed_start():
    with pytest.raises(Exception):
        c.BgmUsageWindowV4(segment_id="w", start=10.0, end=10.0, duration=0.0)


def test_bgm_usage_window_drop_anchor_must_be_inside():
    with pytest.raises(Exception):
        c.BgmUsageWindowV4(
            segment_id="w",
            start=10.0,
            end=20.0,
            duration=10.0,
            drop_anchor_sec=25.0,
        )


def test_annotation_v4_bgm_windows_bounds_enforced():
    meta = c.AnnotationMetaV4(
        asset_id="a",
        case_id="c",
        material_type="bgm",
        duration=60.0,
    )
    with pytest.raises(Exception):
        c.AnnotationV4(
            meta=meta,
            bgm_usage_windows=[
                c.BgmUsageWindowV4(
                    segment_id="w",
                    start=50.0,
                    end=90.0,
                    duration=40.0,
                )
            ],
        )


def test_annotation_v4_bgm_windows_ok_within_bounds():
    meta = c.AnnotationMetaV4(
        asset_id="a",
        case_id="c",
        material_type="bgm",
        duration=60.0,
    )
    ann = c.AnnotationV4(
        meta=meta,
        bgm_usage_windows=[
            c.BgmUsageWindowV4(
                segment_id="w",
                start=10.0,
                end=40.0,
                duration=30.0,
            )
        ],
    )
    assert len(ann.bgm_usage_windows) == 1
