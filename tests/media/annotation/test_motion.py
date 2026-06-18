from __future__ import annotations

import builtins

from packages.core.config import build_settings
from packages.core.contracts import QualityEventType, QualityEventV4
from packages.media.annotation.sensors import (
    classify_window,
    detect_motion_events,
    merge_adjacent_events,
    refine_drop_window,
    summarize_window,
)


def _thresholds() -> dict[str, float]:
    return build_settings().motion_guard.model_dump()


def test_violent_jitter_sequence_classifies_as_hard_shake() -> None:
    pairs = [(10.0 if idx % 2 == 0 else -10.0, 2.0, 0.0) for idx in range(12)]

    metrics = summarize_window(pairs, thresholds=_thresholds())
    event = classify_window(metrics, thresholds=_thresholds(), is_head=False, is_tail=False)

    assert event is not None
    assert event["event_type"] == QualityEventType.shake.value
    assert event["risk_tier"] == "hard"
    QualityEventV4.model_validate(
        {**event, "event_id": "evt_test", "start": 0.0, "end": 1.2, "source": "motion_guard"}
    )


def test_smooth_single_direction_motion_is_suppressed() -> None:
    pairs = [(10.0, 0.2, 0.0) for _ in range(12)]

    metrics = summarize_window(pairs, thresholds=_thresholds())
    event = classify_window(metrics, thresholds=_thresholds(), is_head=False, is_tail=False)

    assert event is None
    assert metrics["straightness_ratio"] >= _thresholds()["smooth_move_straightness"]
    assert metrics["direction_flip_ratio"] <= _thresholds()["smooth_move_flip_ratio"]


def test_tail_vertical_drop_classifies_and_refines_to_drop_subwindow() -> None:
    # A real 收机下坠 is a JITTERY vertical sink (dx flips -> low straightness),
    # not a smooth tilt; leading still frames let refine localize the drop.
    pairs = [(0.0, 0.0, 0.0)] * 3 + [(4.0 * (-1) ** idx, 6.0, 0.0) for idx in range(12)]

    metrics = summarize_window(pairs, thresholds=_thresholds())
    event = classify_window(metrics, thresholds=_thresholds(), is_head=False, is_tail=True)
    refined = refine_drop_window(
        [dy for _dx, dy, _rot in pairs],
        0.0,
        0.1,
        thresholds=_thresholds(),
    )

    assert event is not None
    assert event["event_type"] == QualityEventType.camera_drop.value
    assert event["risk_tier"] == "hard"
    assert refined is not None
    assert 0.0 < refined[0] < refined[1] <= 1.5
    assert refined[1] - refined[0] < 1.5


def test_tail_vertical_drop_accepts_negative_net_y_direction() -> None:
    # Negative net_y (camera drops -> scene moves up) must still fire camera_drop
    # (the abs/direction fix), and it must be JITTERY to clear the smooth gate.
    pairs = [(4.0 * (-1) ** idx, -6.0, 0.0) for idx in range(14)]

    metrics = summarize_window(pairs, thresholds=_thresholds())
    event = classify_window(metrics, thresholds=_thresholds(), is_head=False, is_tail=True)
    refined = refine_drop_window(
        [dy for _dx, dy, _rot in pairs],
        0.0,
        0.1,
        thresholds=_thresholds(),
    )

    assert event is not None
    assert event["event_type"] == QualityEventType.camera_drop.value
    assert "净下沉84.0px" in event["description"]
    assert refined is not None
    assert 0.0 <= refined[0] < refined[1] <= 1.5


def test_smooth_vertical_tilt_not_flagged_as_camera_drop() -> None:
    # A smooth, sustained downward move (constant velocity -> straightness≈1,
    # no direction flips) is a deliberate tilt/crane, NOT a careless drop, even
    # though its vertical magnitude exceeds the camera_drop thresholds. The
    # smooth-move gate must suppress it (no false-positive camera_drop).
    pairs = [(0.5, -6.0, 0.0)] * 13

    metrics = summarize_window(pairs, thresholds=_thresholds())
    event = classify_window(metrics, thresholds=_thresholds(), is_head=False, is_tail=True)

    assert metrics["cum_y_range"] >= _thresholds()["tail_y_range_hard_px"]  # would-be drop
    assert metrics["straightness_ratio"] >= _thresholds()["smooth_move_straightness"]
    assert event is None  # suppressed as intentional smooth motion


def test_sustained_pair_and_duration_boundaries_do_not_misclassify() -> None:
    too_few_pairs = summarize_window(
        [(12.0 if idx % 2 == 0 else -12.0, 0.0, 0.0) for idx in range(7)],
        thresholds=_thresholds(),
    )
    too_short_duration = summarize_window(
        [(12.0 if idx % 2 == 0 else -12.0, 0.0, 0.0) for idx in range(8)],
        thresholds={**_thresholds(), "sample_fps": 12.0},
    )

    assert classify_window(
        too_few_pairs, thresholds=_thresholds(), is_head=False, is_tail=False
    ) is None
    assert classify_window(
        too_short_duration,
        thresholds={**_thresholds(), "sample_fps": 12.0},
        is_head=False,
        is_tail=False,
    ) is None


def test_merge_adjacent_events_merges_same_type_neighbors_only() -> None:
    events = [
        {
            "event_type": QualityEventType.shake.value,
            "start": 0.0,
            "end": 1.0,
            "risk_tier": "soft",
            "confidence": 0.6,
            "severity": 0.4,
            "source": "motion_guard",
            "description": "a",
        },
        {
            "event_type": QualityEventType.shake.value,
            "start": 1.0,
            "end": 2.0,
            "risk_tier": "hard",
            "confidence": 0.8,
            "severity": 0.9,
            "source": "motion_guard",
            "description": "b",
        },
        {
            "event_type": QualityEventType.camera_drop.value,
            "start": 2.0,
            "end": 2.5,
            "risk_tier": "hard",
            "confidence": 0.7,
            "severity": 0.7,
            "source": "motion_guard",
            "description": "c",
        },
    ]

    merged = merge_adjacent_events(events)

    assert len(merged) == 2
    assert merged[0]["event_type"] == QualityEventType.shake.value
    assert merged[0]["start"] == 0.0
    assert merged[0]["end"] == 2.0
    assert merged[0]["risk_tier"] == "hard"
    assert merged[0]["confidence"] == 0.8
    assert merged[0]["severity"] == 0.9
    assert merged[1]["event_type"] == QualityEventType.camera_drop.value


def test_detect_motion_events_returns_empty_when_cv2_import_fails(
    tmp_path, monkeypatch
) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"not a real video")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("cv2 intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert detect_motion_events(str(video_path)) == []
