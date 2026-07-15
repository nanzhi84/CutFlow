"""Tiered emphasis-caption relaxation and the >=5 floor (issue: huazi floor)."""

from __future__ import annotations

import cv2  # type: ignore
import numpy as np  # type: ignore

from packages.production.pipeline import _caption_visual_safety as visual
from packages.production.pipeline.nodes import caption_window_planning as cwp

# --------------------------------------------------------------------------- #
# Pure tier-selection helpers
# --------------------------------------------------------------------------- #


def _opt(option_id: str, anchor: str = "a") -> dict:
    return {
        "caption_option_id": option_id,
        "anchor_id": anchor,
        "safety_envelope": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1},
    }


def _result(specs: list[tuple[str, float, float, float]]) -> visual.OptionMeasurementResult:
    measurements = [
        visual.OptionMeasurement(
            option=_opt(oid), face_overlap=face, scene_text_overlap=text, busy_score=busy
        )
        for oid, face, text, busy in specs
    ]
    return visual.OptionMeasurementResult(measurements=measurements, sample_frames=[1, 2, 3])


def test_tier2_relaxes_scene_text_and_busy_but_never_face():
    result = _result(
        [
            ("clean", 0.0, 0.02, 0.50),  # passes tier 1
            ("texty", 0.0, 0.08, 0.50),  # tier1 rejects (>0.04), tier2 accepts (<0.12)
            ("busy", 0.0, 0.00, 0.78),  # tier1 rejects (>0.72), tier2 accepts (<0.80)
            ("faced", 0.50, 0.00, 0.00),  # face-blocked at every tier
        ]
    )
    tier1, rejected_face, rejected_text, rejected_busy = visual.select_options_at_thresholds(result)
    assert {opt["caption_option_id"] for opt in tier1} == {"clean"}
    assert (rejected_face, rejected_text, rejected_busy) == (1, 1, 1)

    tier2, *_ = visual.select_options_at_thresholds(
        result,
        scene_text_max=visual.EMPHASIS_TIER2_SCENE_TEXT_MAX,
        busy_max=visual.EMPHASIS_TIER2_BUSY_MAX,
    )
    assert {opt["caption_option_id"] for opt in tier2} == {"clean", "texty", "busy"}
    # The face red line does not move when scene-text/busyness relax.
    assert "faced" not in {opt["caption_option_id"] for opt in tier2}


def test_tier3_picks_least_busy_face_clear_option():
    result = _result(
        [
            ("mid", 0.0, 0.30, 0.9),
            ("best", 0.0, 0.20, 0.9),  # lowest scene-text among face-clear
            ("blocked", 0.5, 0.0, 0.0),  # face-blocked despite otherwise clean
        ]
    )
    best = visual.select_best_face_clear_option(result)
    assert best is not None and best["caption_option_id"] == "best"


def test_tier3_returns_none_when_all_face_blocked():
    result = _result([("a", 0.5, 0.0, 0.0), ("b", 0.9, 0.0, 0.0)])
    assert visual.select_best_face_clear_option(result) is None
    assert visual.count_face_blocked(result) == 2


def test_invalid_envelope_never_selected():
    result = visual.OptionMeasurementResult(
        measurements=[
            visual.OptionMeasurement(
                option=_opt("bad"),
                face_overlap=0.0,
                scene_text_overlap=0.0,
                busy_score=0.0,
                valid=False,
            )
        ],
        sample_frames=[1, 2, 3],
    )
    safe, _rf, _rt, rejected_busy = visual.select_options_at_thresholds(result)
    assert safe == [] and rejected_busy == 1
    assert visual.select_best_face_clear_option(result) is None


# --------------------------------------------------------------------------- #
# Node tier orchestration + floor / degradation bookkeeping
# --------------------------------------------------------------------------- #


def _window(event_id: str, *, start: int = 0, end: int = 30) -> dict:
    return {
        "event_id": event_id,
        "text": "限时优惠",
        "start_frame": start,
        "end_frame": end,
        "anchor_candidates": [
            {
                "anchor_id": f"{event_id}__a{index}",
                "rect": {"x": 0.1 + 0.05 * index, "y": 0.5, "w": 0.2, "h": 0.08},
                "text_align": "center",
                "allowed_enter_directions": ["up"],
                "region_tags": ["middle", "center"],
                "max_lines": 1,
                "text_capacity": 8,
            }
            for index in range(3)
        ],
    }


def _diag() -> dict:
    return {
        "generated_anchor_candidates": 0,
        "rejected_anchor_candidates": 0,
        "visual_analysis_failed": False,
        "events_without_options": 0,
        "sampled_frames": 0,
        "rejected_face": 0,
        "rejected_scene_text": 0,
        "rejected_busy": 0,
        "unavailable_detectors": [],
        "safe_anchor_candidates": 0,
        "anchors_pruned_by_cap": 0,
        "options_pruned_by_cap": 0,
        "events_with_options": 0,
        "relaxed_tier2_events": 0,
        "relaxed_tier3_events": 0,
    }


def _fake_measure(metrics_by_event: dict[str, tuple[float, float, float]]):
    def _inner(*, images, sample_frames, option_candidates):
        measurements = [
            visual.OptionMeasurement(
                option=option,
                face_overlap=metrics_by_event[str(option["caption_option_id"]).split("__")[0]][0],
                scene_text_overlap=metrics_by_event[
                    str(option["caption_option_id"]).split("__")[0]
                ][1],
                busy_score=metrics_by_event[str(option["caption_option_id"]).split("__")[0]][2],
            )
            for option in option_candidates
        ]
        return visual.OptionMeasurementResult(
            measurements=measurements, sample_frames=list(sample_frames)
        )

    return _inner


def _analyze(windows, metrics_by_event, monkeypatch, tmp_path):
    monkeypatch.setattr(
        cwp,
        "extract_frames_for_times",
        lambda *a, **k: [(0.0, "f0"), (0.1, "f1"), (0.2, "f2")],
    )
    monkeypatch.setattr(cv2, "imread", lambda _path: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(cwp, "measure_option_candidates", _fake_measure(metrics_by_event))
    diagnostics = _diag()
    summary = cwp._analyze_emphasis_windows(
        video_path="fake.mp4",
        temp_dir=tmp_path,
        fps=30,
        windows=windows,
        diagnostics=diagnostics,
        width=1000,
        height=1000,
        measure=lambda text: len(text) * 30.0,
        font_size=48.0,
        outline=5.0,
        shadow=1.0,
        normal_safe_rect=None,
    )
    return summary, diagnostics


def test_floor_met_at_tier1_needs_no_relaxation(monkeypatch, tmp_path):
    ids = [f"hz_{i:03d}" for i in range(1, 6)]
    windows = [_window(i) for i in ids]
    metrics = {i: (0.0, 0.0, 0.0) for i in ids}
    summary, _diag = _analyze(windows, metrics, monkeypatch, tmp_path)
    assert summary.events_with_options == 5
    assert summary.tier1_events == 5
    assert summary.relaxed_tier2_events == 0
    assert summary.relaxed_tier3_events == 0
    assert all(window["caption_options"] for window in windows)


def test_floor_uses_maximum_feasible_subset_not_raw_option_event_count(monkeypatch, tmp_path):
    ids = [f"hz_{i:03d}" for i in range(1, 6)]
    # All five windows conflict, so the maximum feasible count is one. Once one
    # clean option exists, relaxing four more mutually exclusive events is useless.
    windows = [_window(event_id, start=0, end=30) for event_id in ids]
    metrics = {
        event_id: ((0.0, 0.0, 0.0) if index == 0 else (0.0, 0.08, 0.0))
        for index, event_id in enumerate(ids)
    }
    summary, _diagnostics = _analyze(windows, metrics, monkeypatch, tmp_path)

    assert summary.floor == 1
    assert summary.tier1_events == 1
    assert summary.relaxed_tier2_events == 0
    assert summary.relaxed_tier3_events == 0
    assert summary.events_with_options == 1


def test_below_floor_triggers_tier2_relaxation(monkeypatch, tmp_path):
    ids = [f"hz_{i:03d}" for i in range(1, 6)]
    windows = [_window(i) for i in ids]
    # scene_text 0.08 rejects at tier 1 (>0.04) but clears tier 2 (<0.12).
    metrics = {i: (0.0, 0.08, 0.0) for i in ids}
    summary, diagnostics = _analyze(windows, metrics, monkeypatch, tmp_path)
    assert summary.relaxed_tier2_events == 5
    assert summary.relaxed_tier3_events == 0
    assert summary.events_with_options == 5
    assert diagnostics["relaxed_tier2_events"] == 5


def test_below_floor_falls_through_to_tier3(monkeypatch, tmp_path):
    ids = [f"hz_{i:03d}" for i in range(1, 6)]
    windows = [_window(i) for i in ids]
    # scene_text 0.5 rejects at tier1 and tier2; tier3 ignores it (face clear).
    metrics = {i: (0.0, 0.5, 0.9) for i in ids}
    summary, _diag = _analyze(windows, metrics, monkeypatch, tmp_path)
    assert summary.relaxed_tier3_events == 5
    assert summary.events_with_options == 5
    # Tier 3 collapses each event to a single option.
    assert all(len(window["caption_options"]) == 1 for window in windows)


def test_face_red_line_holds_and_below_floor_is_reported(monkeypatch, tmp_path):
    ids = [f"hz_{i:03d}" for i in range(1, 6)]
    windows = [_window(i) for i in ids]
    # Every option overlaps a face: no tier may place them.
    metrics = {i: (0.5, 0.0, 0.0) for i in ids}
    summary, diagnostics = _analyze(windows, metrics, monkeypatch, tmp_path)
    assert summary.events_with_options == 0
    assert summary.relaxed_tier2_events == 0
    assert summary.relaxed_tier3_events == 0
    assert diagnostics["visual_analysis_failed"] is False
    assert {cause["cause"] for cause in summary.death_causes} == {"face_overlap"}
    assert len(summary.death_causes) == 5


def test_window_too_short_is_not_a_visual_analysis_failure(monkeypatch, tmp_path):
    windows = [_window("hz_001", start=0, end=1)]  # < 3 frames
    summary, diagnostics = _analyze(windows, {"hz_001": (0.0, 0.0, 0.0)}, monkeypatch, tmp_path)
    assert diagnostics["visual_analysis_failed"] is False
    assert "window_too_short" in diagnostics["unavailable_detectors"]
    assert summary.death_causes == [{"event_id": "hz_001", "cause": "window_too_short"}]
    assert windows[0]["caption_options"] == []
