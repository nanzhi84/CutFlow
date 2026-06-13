from __future__ import annotations

import pytest

from packages.core.contracts import AnalysisWindow, SpeechIslandV4, WindowReason
from packages.media.annotation import plan_windows


def _assert_gapless_cover(windows, duration):
    assert windows, "expected at least one window"
    assert windows[0].start == pytest.approx(0.0, abs=1e-6)
    assert windows[-1].end == pytest.approx(duration, abs=1e-6)
    for a, b in zip(windows, windows[1:]):
        assert a.end == pytest.approx(b.start, abs=1e-6)  # no gap, no overlap
    for w in windows:
        assert w.end > w.start  # non-empty


def test_plan_windows_empty_duration():
    assert plan_windows(duration=0.0, shot_cuts=[], speech_islands=None) == []


def test_plan_windows_single_take_within_max_is_one_window():
    windows = plan_windows(
        duration=8.0, shot_cuts=[], speech_islands=None, window_max_sec=10.0
    )
    assert len(windows) == 1
    assert windows[0].reason == WindowReason.scene_boundary
    _assert_gapless_cover(windows, 8.0)


def test_plan_windows_cutless_long_take_mechanical_gapless():
    duration = 25.0
    windows = plan_windows(
        duration=duration, shot_cuts=[], speech_islands=None, window_max_sec=10.0
    )
    _assert_gapless_cover(windows, duration)
    assert all(isinstance(w, AnalysisWindow) for w in windows)
    assert all(w.reason == WindowReason.mechanical for w in windows)


def test_plan_windows_cutless_snaps_to_vad_boundary():
    # Mechanical cut at 10.0; an island ending at 9.7 (within +/-0.5) pulls the cut.
    islands = [SpeechIslandV4(start=2.0, end=9.7, confidence=0.9)]
    windows = plan_windows(
        duration=20.0,
        shot_cuts=[],
        speech_islands=islands,
        window_max_sec=10.0,
        vad_adhesion_range=0.5,
    )
    _assert_gapless_cover(windows, 20.0)
    snapped = [w for w in windows if w.end == pytest.approx(9.7, abs=1e-6)]
    assert snapped and snapped[0].reason == WindowReason.vad_snapped


def test_plan_windows_with_cuts_merges_short_and_covers():
    # Fast cuts every 1s up to 9s on a 10s clip; window_min_sec=3 merges shorts.
    cuts = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    windows = plan_windows(
        duration=10.0,
        shot_cuts=cuts,
        speech_islands=None,
        window_min_sec=3.0,
        window_max_sec=10.0,
    )
    _assert_gapless_cover(windows, 10.0)
    assert any(w.reason == WindowReason.merged_short for w in windows)


def test_plan_windows_long_single_scene_split():
    # One scene boundary at 12s on a 24s clip; each half exceeds max -> split.
    windows = plan_windows(
        duration=24.0,
        shot_cuts=[12.0],
        speech_islands=None,
        window_min_sec=3.0,
        window_max_sec=10.0,
    )
    _assert_gapless_cover(windows, 24.0)
    assert any(w.reason == WindowReason.long_scene_split for w in windows)


def test_plan_windows_accepts_dict_islands():
    windows = plan_windows(
        duration=20.0,
        shot_cuts=[],
        speech_islands=[{"start": 1.0, "end": 9.6}],
        window_max_sec=10.0,
        vad_adhesion_range=0.5,
    )
    _assert_gapless_cover(windows, 20.0)
