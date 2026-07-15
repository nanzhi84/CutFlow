from __future__ import annotations

import builtins
from types import SimpleNamespace

from packages.core.config import build_settings
from packages.core.contracts import QualityEventType, QualityEventV4
from packages.media.annotation.sensors import (
    classify_window,
    detect_motion_events,
    merge_adjacent_events,
    refine_drop_window,
    summarize_window,
)
from packages.media.annotation.sensors import motion as motion_mod


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


def test_motion_empty_and_invalid_inputs_fail_open(tmp_path) -> None:
    summary = summarize_window([], thresholds=_thresholds())

    assert summary["pairs"] == 0
    assert summary["duration_sec"] == 0.0
    assert classify_window({"pairs": "bad", "duration_sec": 1.0}, thresholds=_thresholds(), is_head=False, is_tail=False) is None
    assert refine_drop_window([1, 2, 3], 0.0, 0.1, thresholds=_thresholds()) is None
    assert refine_drop_window([1, 2, 3, 4], 0.0, 0.0, thresholds=_thresholds()) is None
    assert refine_drop_window([0.1, -0.1, 0.1, -0.1], 0.0, 0.1, thresholds=_thresholds()) is None
    assert refine_drop_window([-2, -2, 1, -2, -2], 0.0, 0.1, thresholds=_thresholds()) is not None
    assert detect_motion_events(str(tmp_path / "missing.mp4")) == []
    assert merge_adjacent_events([{"start": 2.0, "end": 1.0, "event_type": "shake"}]) == []


def test_classify_time_axis_builds_refined_camera_drop_event() -> None:
    thresholds = {
        **_thresholds(),
        "sample_fps": 10.0,
        "window_sec": 1.4,
        "hop_sec": 1.4,
        "active_px": 1.0,
        "hard_px": 2.0,
        "p95_hard_px": 4.0,
        "tail_y_range_hard_px": 20.0,
        "tail_net_y_hard_px": 20.0,
        "smooth_move_straightness": 0.98,
        "smooth_move_flip_ratio": 0.05,
    }
    pair_estimates = [
        {"time": round((idx + 1) * 0.1, 2), "dx": 4.0 * (-1) ** idx, "dy": 6.0}
        for idx in range(14)
    ]

    events = motion_mod._classify_time_axis(pair_estimates, 1.4, thresholds)

    assert len(events) == 1
    assert events[0]["event_type"] == QualityEventType.camera_drop.value
    assert events[0]["source"] == "motion_guard"
    assert events[0]["start"] >= 0.0
    assert events[0]["end"] <= 1.4
    assert "收机下坠" in events[0]["description"]


def test_read_motion_frames_fail_open_and_decodes_raw_frames(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")

    class ClosedCapture:
        def __init__(self, _path):
            pass

        def isOpened(self):
            return False

        def release(self):
            return None

    fake_cv2 = SimpleNamespace(VideoCapture=ClosedCapture)
    assert motion_mod._read_motion_frames(fake_cv2, object(), str(video), sample_fps=2, width=4) == ([], 0.0)

    class BadDimsCapture:
        def __init__(self, _path):
            pass

        def isOpened(self):
            return True

        def get(self, _prop):
            return 0

        def release(self):
            return None

    fake_cv2 = SimpleNamespace(
        VideoCapture=BadDimsCapture,
        CAP_PROP_FRAME_WIDTH=1,
        CAP_PROP_FRAME_HEIGHT=2,
        CAP_PROP_FPS=3,
        CAP_PROP_FRAME_COUNT=4,
    )
    assert motion_mod._read_motion_frames(fake_cv2, object(), str(video), sample_fps=2, width=4) == ([], 0.0)

    class GoodCapture:
        def __init__(self, _path):
            pass

        def isOpened(self):
            return True

        def get(self, prop):
            return {1: 4, 2: 2, 3: 2, 4: 4}[prop]

        def release(self):
            return None

    class FakeArray:
        def __init__(self, values):
            self.values = values

        def reshape(self, shape):
            frame_count, height, width = shape
            frames = []
            offset = 0
            for _ in range(frame_count):
                frames.append(SimpleNamespace(shape=(height, width), data=self.values[offset : offset + height * width]))
                offset += height * width
            return frames

    fake_np = SimpleNamespace(uint8="uint8", frombuffer=lambda data, dtype: FakeArray(list(data)))
    fake_cv2 = SimpleNamespace(
        VideoCapture=GoodCapture,
        CAP_PROP_FRAME_WIDTH=1,
        CAP_PROP_FRAME_HEIGHT=2,
        CAP_PROP_FPS=3,
        CAP_PROP_FRAME_COUNT=4,
        GaussianBlur=lambda gray, _kernel, _sigma: ("blurred", gray),
    )

    monkeypatch.setattr(
        motion_mod.FfmpegRunner,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=bytes(range(16)), stderr=b""),
    )

    frames, duration = motion_mod._read_motion_frames(fake_cv2, fake_np, str(video), sample_fps=2, width=4)

    assert duration == 2.0
    assert [time for time, _frame in frames] == [0.0, 0.5]
    assert frames[0][1][0] == "blurred"

    monkeypatch.setattr(
        motion_mod.FfmpegRunner,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=b"", stderr=b"decode failed"),
    )
    assert motion_mod._read_motion_frames(fake_cv2, fake_np, str(video), sample_fps=2, width=4)[0] == []


def test_estimate_pair_and_numeric_helpers():
    class FakeNp:
        uint8 = "uint8"

        @staticmethod
        def zeros(shape, _dtype):
            _ = shape
            return FakeMask()

        @staticmethod
        def median(values, axis=0):
            _ = axis
            return (1.5, -2.5)

    class FakeFrame:
        shape = (10, 10)

    class FakeMask:
        def __setitem__(self, _key, _value):
            return None

    class FakeMatrix:
        def __getitem__(self, key):
            row, col = key
            return [[0, 0, 3.0], [0, 0, -4.0]][row][col]

    class FakeCv2:
        RANSAC = 1
        TERM_CRITERIA_EPS = 2
        TERM_CRITERIA_COUNT = 4

        def __init__(self, *, enough_points=True, matrix=None):
            self.enough_points = enough_points
            self.matrix = matrix

        def goodFeaturesToTrack(self, *_args, **_kwargs):
            if not self.enough_points:
                return None
            return FakePoints(12)

        def calcOpticalFlowPyrLK(self, *_args, **_kwargs):
            return FakePoints(12, offset=(1.0, 2.0)), FakeStatus(12), None

        def estimateAffinePartial2D(self, *_args, **_kwargs):
            return self.matrix, None

    class FakePoints:
        def __init__(self, count, *, offset=(0.0, 0.0)):
            self.points = [(float(idx) + offset[0], float(idx) + offset[1]) for idx in range(count)]

        def __len__(self):
            return len(self.points)

        def __getitem__(self, _mask):
            return self

        def reshape(self, *_shape):
            return self.points

    class FakeStatus:
        def __init__(self, count):
            self.count = count

        def ravel(self):
            return [1] * self.count

    assert motion_mod._estimate_pair(FakeCv2(enough_points=False), FakeNp, FakeFrame(), FakeFrame()) is None
    assert motion_mod._estimate_pair(FakeCv2(matrix=FakeMatrix()), FakeNp, FakeFrame(), FakeFrame()) == {
        "dx": 3.0,
        "dy": -4.0,
    }
    assert motion_mod._coerce_pair({"dx": "2.5", "dy": "bad"}) == (2.5, 0.0)
    assert motion_mod._coerce_pair((1.0,)) == (0.0, 0.0)
    assert motion_mod._percentile([], 95) == 0.0
    assert motion_mod._percentile([2], 95) == 2
    assert motion_mod._median([1, 3, 5]) == 3
    assert motion_mod._cumsum([1, -2, 3]) == [1.0, -1.0, 2.0]
    assert motion_mod._max_true_run([False, True, True, False, True]) == 2
    assert motion_mod._true_runs([True, True, False, True]) == [(0, 1), (3, 3)]
    assert motion_mod._direction_flip_stats([0, 1, -1, 1]) == (2, 3)
    assert motion_mod._floor_time(1.23, {**_thresholds(), "refine_round_sec": 0.1}) == 1.2
    assert motion_mod._ceil_time(1.21, {**_thresholds(), "refine_round_sec": 0.1}) == 1.3
    assert motion_mod._as_float("bad", 7.0) == 7.0
    assert motion_mod._clamp(2.0, 0.0, 1.0) == 1.0
