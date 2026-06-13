from __future__ import annotations

from packages.media.annotation.sensors import MotionGuard
from tests.media.annotation.fixtures import make_multi_cut_video


def test_motion_guard_windows_only_portrait_edges():
    mg = MotionGuard(portrait_edge_window=1.5)
    # Non-portrait -> no windows.
    assert mg.motion_guard_windows(total_duration=10.0, video_type="scenery") == []
    # Too short -> no windows.
    assert mg.motion_guard_windows(total_duration=0.5, video_type="portrait") == []
    # Portrait long enough -> head + tail.
    windows = mg.motion_guard_windows(total_duration=10.0, video_type="portrait")
    labels = [w[0] for w in windows]
    assert "head" in labels and "tail" in labels


# Base tail metrics describing a sustained vertical sink whose *step* motion is
# below the high_step gate (hard_ratio < 0.55), so the camera_drop verdict is
# driven solely by the vertical_drop gate. This isolates origin's calibrated
# y-thresholds (tail_y_range_hard_px=70, tail_net_y_hard_px=65) instead of letting
# the alternate "high_step_motion and y_range>=55" path mask threshold drift.
_VERTICAL_SINK_BASE = {
    "start": 8.0,
    "end": 10.0,
    "pairs": 20,
    "mag_p95_px360": 12.0,
    "active_ratio": 0.9,
    "hard_ratio": 0.3,  # below high_step gate (0.55) -> only vertical_drop can fire
    "max_active_run_pairs": 18,
    "cum_x_range_px360": 10.0,
    "straightness_ratio": 0.5,
    "direction_flip_ratio": 0.1,
    "jerk_p90_px360": 3.0,
    "residual_to_p95_ratio": 0.4,
}


def test_build_event_camera_drop_from_metrics():
    mg = MotionGuard()
    # The vertical cumulative range / net displacement must clear origin's
    # calibrated vertical_drop gate so this fires camera_drop. If the thresholds
    # ever drift, the paired below-gate test will catch it.
    metrics = {**_VERTICAL_SINK_BASE, "cum_y_range_px360": 80.0, "net_y_px360": 72.0}
    # Pin the inputs to origin's effective calibration: the case clears 70/65 and
    # the asserted threshold values themselves guard against silent default drift.
    assert mg.motion_guard_tail_y_range_hard_px == 70.0
    assert mg.motion_guard_tail_net_y_hard_px == 65.0
    assert metrics["cum_y_range_px360"] >= mg.motion_guard_tail_y_range_hard_px
    assert metrics["net_y_px360"] >= mg.motion_guard_tail_net_y_hard_px
    event = mg.build_motion_guard_event_from_metrics(
        metrics, label="tail", total_duration=10.0
    )
    assert event is not None
    assert event["event_type"] == "camera_drop"
    assert event["risk_tier"] == "hard"
    assert event["source"] == "motion_guard"


def test_build_event_camera_drop_below_origin_vertical_gate_returns_none():
    # The drifted-default inputs the old test used (y_range=60, net_y=40) sit below
    # origin's calibrated vertical_drop gate (>=70 / >=65). With high_step motion
    # disabled, the only path to camera_drop is vertical_drop, so this must NOT
    # fire. If the thresholds drift looser (e.g. back to 40/26), this fails -> the
    # regression guard that would catch future calibration drift.
    mg = MotionGuard()
    metrics = {**_VERTICAL_SINK_BASE, "cum_y_range_px360": 60.0, "net_y_px360": 40.0}
    assert metrics["cum_y_range_px360"] < mg.motion_guard_tail_y_range_hard_px
    assert metrics["net_y_px360"] < mg.motion_guard_tail_net_y_hard_px
    assert mg.build_motion_guard_event_from_metrics(
        metrics, label="tail", total_duration=10.0
    ) is None


def test_build_event_returns_none_for_calm_metrics():
    mg = MotionGuard()
    calm = {
        "start": 0.0,
        "end": 2.0,
        "pairs": 20,
        "mag_p95_px360": 1.0,
        "active_ratio": 0.05,
        "hard_ratio": 0.0,
        "max_active_run_pairs": 1,
        "cum_x_range_px360": 2.0,
        "cum_y_range_px360": 2.0,
        "net_y_px360": 1.0,
        "straightness_ratio": 0.9,
        "direction_flip_ratio": 0.05,
        "jerk_p90_px360": 0.5,
        "residual_to_p95_ratio": 0.2,
    }
    assert mg.build_motion_guard_event_from_metrics(
        calm, label="head", total_duration=10.0
    ) is None


def test_build_event_requires_minimum_pairs_and_duration():
    mg = MotionGuard()
    too_few = {"start": 0.0, "end": 2.0, "pairs": 2}
    assert mg.build_motion_guard_event_from_metrics(
        too_few, label="head", total_duration=10.0
    ) is None


def test_detect_motion_guard_events_no_false_positive_on_static_clip(tmp_path):
    # A solid-color (static) clip has no real camera motion -> no events.
    video = make_multi_cut_video(tmp_path, seg_dur=2.0, width=320, height=568)
    mg = MotionGuard()
    result = mg.detect_motion_guard_events(
        str(video), total_duration=6.0, video_type="portrait"
    )
    assert result["enabled"] is True
    assert result["events"] == []


def test_detect_motion_guard_disabled_returns_empty():
    mg = MotionGuard(motion_guard_enabled=False)
    result = mg.detect_motion_guard_events(
        "irrelevant.mp4", total_duration=10.0, video_type="portrait"
    )
    assert result["events"] == []
    assert result["windows"] == []
